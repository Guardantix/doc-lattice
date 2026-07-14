#!/usr/bin/env python3
"""Generate Python slug compatibility data from pinned upstream behavior.

Node evaluates the exact upstream slug operation and casing properties over every Unicode scalar
value. Python 3.13 supplies the minimum supported lowercase table so the generated artifact can
patch version gaps.
"""

import argparse
import hashlib
import json
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

UPSTREAM_VERSION = "2.0.0"
UPSTREAM_JAVASCRIPT_UNICODE = "17.0"
PYTHON_BASELINE_UNICODE = "15.1.0"
CHECKED_UNICODE_SCALARS = 1_112_064
_MAX_UNICODE = 0x10FFFF
_MAX_BMP = 0xFFFF
_SURROGATE_START = 0xD800
_SURROGATE_END = 0xDFFF
_LOWERCASE_MAPPING_FIELDS = 2
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUTPUT = _REPO_ROOT / "src" / "doc_lattice" / "_github_slugger_data.py"

_NODE_PROGRAM = r"""
import {pathToFileURL} from 'node:url'

const regexUrl = pathToFileURL(process.argv[1])
const module = await import(regexUrl.href)
const slugger = await import(new URL('index.js', regexUrl).href)
const regex = module.regex
const casedRegex = /^\p{Cased}$/u
const caseIgnorableRegex = /^\p{Case_Ignorable}$/u
const stripped = []
const lowercase = []
const cased = []
const caseIgnorable = []
let slugOperations = 0
for (let codePoint = 0; codePoint <= 0x10FFFF; codePoint++) {
  if (codePoint >= 0xD800 && codePoint <= 0xDFFF) continue
  const value = String.fromCodePoint(codePoint)
  const lower = value.toLowerCase()
  if (lower !== value) {
    lowercase.push([codePoint, [...lower].map(character => character.codePointAt(0))])
  }
  if (casedRegex.test(value)) cased.push(codePoint)
  if (caseIgnorableRegex.test(value)) caseIgnorable.push(codePoint)
  regex.lastIndex = 0
  const composed = lower.replace(regex, '').replace(/ /g, '-')
  if (slugger.slug(value) !== composed) {
    throw new Error(`slug mismatch at U+${codePoint.toString(16)}`)
  }
  slugOperations++
  regex.lastIndex = 0
  if (regex.test(value)) stripped.push(codePoint)
}
for (const value of ['ΟΣ', 'A B', 'İ', 'ꟋΣ', '\u{1C89}Σ', 'AΣ\u{1AD0}A']) {
  regex.lastIndex = 0
  const composed = value.toLowerCase().replace(regex, '').replace(/ /g, '-')
  if (slugger.slug(value) !== composed) throw new Error(`contextual slug mismatch for ${value}`)
  slugOperations++
}
process.stdout.write(JSON.stringify({
  node: process.versions.node,
  unicode: process.versions.unicode,
  stripped,
  lowercase,
  cased,
  caseIgnorable,
  slugOperations,
}))
"""

_PYTHON_PROGRAM = r"""
import json
import unicodedata

lowercase = []
for code_point in range(0x110000):
    if 0xD800 <= code_point <= 0xDFFF:
        continue
    value = chr(code_point)
    lower = value.lower()
    if lower != value:
        lowercase.append([code_point, [ord(character) for character in lower]])
print(json.dumps({"unicode": unicodedata.unidata_version, "lowercase": lowercase}))
"""


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """Provenance and exhaustive counts rendered into the generated artifact."""

    version: str
    regex_sha256: str
    stripped_count: int
    javascript_unicode: str
    python_baseline_unicode: str
    upstream_lowercase_count: int
    slug_operation_count: int
    cased_count: int
    case_ignorable_count: int


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


def _escape_string_code_point(code_point: int) -> str:
    if code_point <= _MAX_BMP:
        return f"\\u{code_point:04x}"
    return f"\\U{code_point:08x}"


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


def _render_wrapped_pattern(pattern: str) -> str:
    chunks: list[str] = []
    offset = 0
    while offset < len(pattern):
        end = min(offset + 80, len(pattern))
        if pattern[end - 1] == "\\":
            end -= 1
        chunks.append(pattern[offset:end])
        offset = end
    return "".join(f'    r"{chunk}"\n' for chunk in chunks)


def render_module(
    pattern: str,
    lowercase_patches: Sequence[tuple[int, Sequence[int]]],
    metadata: ArtifactMetadata,
    *,
    cased_pattern: str,
    case_ignorable_pattern: str,
) -> str:
    """Render the generated Python module.

    Args:
        pattern: Generated Python regular-expression pattern.
        lowercase_patches: JavaScript mappings absent from the Python baseline.
        metadata: Upstream versions, integrity hash, and exhaustive operation counts.
        cased_pattern: JavaScript Unicode ``Cased`` property as an explicit pattern.
        case_ignorable_pattern: JavaScript Unicode ``Case_Ignorable`` property pattern.

    Returns:
        Complete deterministic Python module text.
    """
    pattern_lines = _render_wrapped_pattern(pattern)
    lowercase_lines = "".join(
        f'    0x{source:06X}: "'
        + "".join(_escape_string_code_point(code_point) for code_point in replacement)
        + '",\n'
        for source, replacement in lowercase_patches
    )
    patch_pattern = render_pattern(coalesce(source for source, _ in lowercase_patches))
    patch_pattern_lines = _render_wrapped_pattern(patch_pattern)
    cased_pattern_lines = _render_wrapped_pattern(cased_pattern)
    case_ignorable_pattern_lines = _render_wrapped_pattern(case_ignorable_pattern)
    return (
        '"""Generated compatibility data for github-slugger. Do not edit by hand."""\n\n'
        f'UPSTREAM_PACKAGE = "github-slugger@{metadata.version}"\n'
        f'JAVASCRIPT_UNICODE_VERSION = "{metadata.javascript_unicode}"\n'
        f'PYTHON_BASELINE_UNICODE_VERSION = "{metadata.python_baseline_unicode}"\n'
        "UPSTREAM_REGEX_SHA256 = (\n"
        f'    "{metadata.regex_sha256}"  # pragma: allowlist secret\n'
        ")\n"
        f"CHECKED_UNICODE_SCALARS = {CHECKED_UNICODE_SCALARS:_}\n"
        f"STRIPPED_UNICODE_SCALARS = {metadata.stripped_count:_}\n"
        f"UPSTREAM_LOWERCASE_MAPPINGS = {metadata.upstream_lowercase_count:_}\n"
        f"LOWERCASE_PATCH_MAPPINGS = {len(lowercase_patches):_}\n"
        f"CHECKED_SLUG_OPERATIONS = {metadata.slug_operation_count:_}\n"
        f"CASED_UNICODE_SCALARS = {metadata.cased_count:_}\n"
        f"CASE_IGNORABLE_UNICODE_SCALARS = {metadata.case_ignorable_count:_}\n"
        f"LOWERCASE_PATCH_TRANSLATION = {{\n{lowercase_lines}}}\n"
        f"LOWERCASE_PATCH_PATTERN = (\n{patch_pattern_lines})\n"
        f"CASED_PATTERN = (\n{cased_pattern_lines})\n"
        f"CASE_IGNORABLE_PATTERN = (\n{case_ignorable_pattern_lines})\n"
        f"SLUG_STRIP_PATTERN = (\n{pattern_lines})\n"
    )


def _parse_lowercase_mappings(values: object, *, source: str) -> list[tuple[int, tuple[int, ...]]]:
    if not isinstance(values, list):
        msg = f"{source} lowercase evaluator returned a non-list result"
        raise ValueError(msg)
    mappings: list[tuple[int, tuple[int, ...]]] = []
    for mapping in values:
        if not isinstance(mapping, list) or len(mapping) != _LOWERCASE_MAPPING_FIELDS:
            msg = f"{source} lowercase evaluator returned a malformed mapping"
            raise ValueError(msg)
        code_point, raw_replacement = mapping
        if not isinstance(code_point, int) or not isinstance(raw_replacement, list):
            msg = f"{source} lowercase evaluator returned a malformed mapping"
            raise ValueError(msg)
        replacement: list[int] = []
        for value in raw_replacement:
            if not isinstance(value, int):
                msg = f"{source} lowercase evaluator returned a malformed mapping"
                raise ValueError(msg)
            replacement.append(value)
        mappings.append((code_point, tuple(replacement)))
    return mappings


def _evaluate_upstream(
    regex_path: Path,
) -> tuple[
    list[int],
    list[tuple[int, tuple[int, ...]]],
    list[int],
    list[int],
    str,
    str,
    int,
]:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", _NODE_PROGRAM, str(regex_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        msg = "upstream evaluator returned a non-object result"
        raise ValueError(msg)
    stripped = data.get("stripped")
    lowercase = data.get("lowercase")
    cased = data.get("cased")
    case_ignorable = data.get("caseIgnorable")
    node_version = data.get("node")
    unicode_version = data.get("unicode")
    slug_operations = data.get("slugOperations")
    if not isinstance(stripped, list) or not all(isinstance(value, int) for value in stripped):
        msg = "upstream regex evaluator returned a non-integer result"
        raise ValueError(msg)
    if not isinstance(cased, list) or not all(isinstance(value, int) for value in cased):
        msg = "upstream cased-property evaluator returned a non-integer result"
        raise ValueError(msg)
    if not isinstance(case_ignorable, list) or not all(
        isinstance(value, int) for value in case_ignorable
    ):
        msg = "upstream case-ignorable evaluator returned a non-integer result"
        raise ValueError(msg)
    mappings = _parse_lowercase_mappings(lowercase, source="upstream")
    if not isinstance(node_version, str) or not isinstance(unicode_version, str):
        msg = "upstream evaluator omitted runtime provenance"
        raise ValueError(msg)
    if not isinstance(slug_operations, int) or slug_operations < CHECKED_UNICODE_SCALARS:
        msg = "upstream evaluator returned an invalid slug-operation count"
        raise ValueError(msg)
    return (
        stripped,
        mappings,
        cased,
        case_ignorable,
        node_version,
        unicode_version,
        slug_operations,
    )


def _evaluate_python_lowercase(
    python_executable: str,
) -> tuple[list[tuple[int, tuple[int, ...]]], str]:
    result = subprocess.run(
        [python_executable, "-c", _PYTHON_PROGRAM],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        msg = "Python baseline evaluator returned a non-object result"
        raise ValueError(msg)
    unicode_version = data.get("unicode")
    if not isinstance(unicode_version, str):
        msg = "Python baseline evaluator omitted Unicode provenance"
        raise ValueError(msg)
    return _parse_lowercase_mappings(data.get("lowercase"), source="Python"), unicode_version


def derive_lowercase_patches(
    upstream: Sequence[tuple[int, tuple[int, ...]]],
    baseline: Sequence[tuple[int, tuple[int, ...]]],
) -> list[tuple[int, tuple[int, ...]]]:
    """Return post-lowercase patches that make the baseline match upstream.

    Args:
        upstream: JavaScript lowercase mappings for changed scalars.
        baseline: Minimum-supported Python lowercase mappings for changed scalars.

    Returns:
        Ordered mappings to apply after the baseline lowercase operation.

    Raises:
        ValueError: If scalar translation cannot reproduce the upstream mapping.
    """
    upstream_by_source = dict(upstream)
    baseline_by_source = dict(baseline)
    patches: dict[int, tuple[int, ...]] = {}
    for code_point in range(_MAX_UNICODE + 1):
        if _SURROGATE_START <= code_point <= _SURROGATE_END:
            continue
        baseline_result = baseline_by_source.get(code_point, (code_point,))
        patched_result = tuple(
            mapped
            for value in baseline_result
            for mapped in upstream_by_source.get(value, (value,))
        )
        upstream_result = upstream_by_source.get(code_point, (code_point,))
        if patched_result != upstream_result:
            msg = f"cannot patch Python lowercase to upstream at U+{code_point:04X}"
            raise ValueError(msg)
        for value in baseline_result:
            replacement = upstream_by_source.get(value)
            if replacement is not None:
                patches[value] = replacement
    return sorted(patches.items())


def _validate_unicode_property_values(values: Sequence[int], *, property_name: str) -> None:
    if len(values) > CHECKED_UNICODE_SCALARS:
        msg = f"upstream {property_name} property exceeds the Unicode scalar set"
        raise ValueError(msg)
    if any(current >= following for current, following in pairwise(values)):
        msg = f"upstream {property_name} property values are not unique and ordered"
        raise ValueError(msg)
    if any(value < 0 or value > _MAX_UNICODE for value in values):
        msg = f"upstream {property_name} property contains a value outside the Unicode range"
        raise ValueError(msg)
    if any(_SURROGATE_START <= value <= _SURROGATE_END for value in values):
        msg = f"upstream {property_name} property contains a surrogate code point"
        raise ValueError(msg)


def _render_from_package(
    package_root: Path, python_executable: str
) -> tuple[str, int, int, int, int, str, str]:
    package_data = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    version = package_data.get("version")
    if version != UPSTREAM_VERSION:
        msg = f"expected github-slugger@{UPSTREAM_VERSION}, found {version!r}"
        raise ValueError(msg)

    regex_path = package_root / "regex.js"
    regex_bytes = regex_path.read_bytes()
    regex_sha256 = hashlib.sha256(regex_bytes).hexdigest()
    (
        stripped,
        lowercase,
        cased,
        case_ignorable,
        node_version,
        unicode_version,
        slug_operations,
    ) = _evaluate_upstream(regex_path)
    if unicode_version != UPSTREAM_JAVASCRIPT_UNICODE:
        msg = (
            f"expected JavaScript Unicode {UPSTREAM_JAVASCRIPT_UNICODE}, "
            f"found {unicode_version!r} from Node {node_version}"
        )
        raise ValueError(msg)
    baseline_lowercase, baseline_unicode = _evaluate_python_lowercase(python_executable)
    if baseline_unicode != PYTHON_BASELINE_UNICODE:
        msg = (
            f"expected Python baseline Unicode {PYTHON_BASELINE_UNICODE}, "
            f"found {baseline_unicode!r} from {python_executable}"
        )
        raise ValueError(msg)
    if len(stripped) > CHECKED_UNICODE_SCALARS:
        msg = "upstream regex matched more values than the Unicode scalar set"
        raise ValueError(msg)
    if any(_SURROGATE_START <= value <= _SURROGATE_END for value in stripped):
        msg = "upstream evaluator unexpectedly returned a surrogate code point"
        raise ValueError(msg)
    if stripped and (stripped[0] < 0 or stripped[-1] > _MAX_UNICODE):
        msg = "upstream evaluator returned a value outside the Unicode range"
        raise ValueError(msg)
    if len(lowercase) > CHECKED_UNICODE_SCALARS:
        msg = "upstream lowercase mapping exceeds the Unicode scalar set"
        raise ValueError(msg)
    sources = [source for source, _ in lowercase]
    if sources != sorted(set(sources)):
        msg = "upstream lowercase mappings are not unique and ordered"
        raise ValueError(msg)
    mapped_values = [value for _, replacement in lowercase for value in replacement]
    all_values = sources + mapped_values
    if any(value < 0 or value > _MAX_UNICODE for value in all_values):
        msg = "upstream lowercase mapping contains a value outside the Unicode range"
        raise ValueError(msg)
    if any(_SURROGATE_START <= value <= _SURROGATE_END for value in all_values):
        msg = "upstream lowercase mapping contains a surrogate code point"
        raise ValueError(msg)
    for values, property_name in (
        (cased, "cased"),
        (case_ignorable, "case-ignorable"),
    ):
        _validate_unicode_property_values(values, property_name=property_name)

    lowercase_patches = derive_lowercase_patches(lowercase, baseline_lowercase)
    pattern = render_pattern(coalesce(stripped))
    cased_pattern = render_pattern(coalesce(cased))
    case_ignorable_pattern = render_pattern(coalesce(case_ignorable))
    metadata = ArtifactMetadata(
        version=version,
        regex_sha256=regex_sha256,
        stripped_count=len(stripped),
        javascript_unicode=unicode_version,
        python_baseline_unicode=baseline_unicode,
        upstream_lowercase_count=len(lowercase),
        slug_operation_count=slug_operations,
        cased_count=len(cased),
        case_ignorable_count=len(case_ignorable),
    )
    rendered = render_module(
        pattern,
        lowercase_patches,
        metadata,
        cased_pattern=cased_pattern,
        case_ignorable_pattern=case_ignorable_pattern,
    )
    return (
        rendered,
        len(stripped),
        len(lowercase),
        len(lowercase_patches),
        slug_operations,
        regex_sha256,
        node_version,
    )


def _install_package(working_dir: Path) -> Path:
    try:
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
    except FileNotFoundError as exc:
        msg = "npm executable not found; install Node.js and npm to generate slug data"
        raise RuntimeError(msg) from exc
    return working_dir / "node_modules" / "github-slugger"


def _write_or_check(
    package_root: Path,
    output: Path,
    *,
    check: bool,
    python_executable: str,
) -> tuple[int, str]:
    (
        rendered,
        stripped_count,
        lowercase_count,
        lowercase_patch_count,
        slug_operation_count,
        regex_sha256,
        node_version,
    ) = _render_from_package(package_root, python_executable)
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
        f"node={node_version}, javascript_unicode={UPSTREAM_JAVASCRIPT_UNICODE}, "
        f"python_unicode={PYTHON_BASELINE_UNICODE}, "
        f"checked_scalars={CHECKED_UNICODE_SCALARS}, stripped={stripped_count}, "
        f"lowercase={lowercase_count}, patches={lowercase_patch_count}, "
        f"slug_operations={slug_operation_count}, "
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
    parser.add_argument(
        "--python-baseline",
        default="python3.13",
        help="Python executable with the minimum supported Unicode table",
    )
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    """Generate or verify the pinned slug compatibility artifact."""
    args = _parse_args()
    if args.package_root is not None:
        status, _ = _write_or_check(
            args.package_root,
            args.output,
            check=args.check,
            python_executable=args.python_baseline,
        )
        return status
    with tempfile.TemporaryDirectory(prefix="doc-lattice-slugger-") as tmp:
        package_root = _install_package(Path(tmp))
        status, _ = _write_or_check(
            package_root,
            args.output,
            check=args.check,
            python_executable=args.python_baseline,
        )
        return status


if __name__ == "__main__":
    raise SystemExit(main())
