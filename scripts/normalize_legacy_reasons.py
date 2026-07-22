"""Record the normalized legacy replay baseline for the successor replay gate (spec S6.4).

The replay gate compares, per replay-inventory entry, the tuple (status, retained invocation
tuples, reason category). The baseline scanner's ``incomplete_reason`` is unstructured English
prose, so this script freezes each entry's baseline tuple at commit ``be4b7b1`` and maps every
distinct ``incomplete_reason`` string to a stable successor reason category through one explicit,
static mapping. The mapping is embedded in the artifact so the future gate harness never
re-infers a category from a legacy error substring (S6.4).

The mapping is total for the strings the 580-entry inventory produces: an ``incomplete_reason``
absent from ``_REASON_MAP`` or ``_CONTEXTUAL_PINS`` aborts generation rather than falling into a
silent bucket, so any future scanner string is a deliberate addition. Legacy scanner resource
bounds that no successor code models are recorded under ``legacy_only_categories``.

Four legacy strings collapse several successor-distinct constructs into one bucket, so they are
classified per entry from the entry's source text rather than mapped globally (S6.4: adjudication
happens now, at generation, never at gate time). ``_classify_contextual`` inspects the leading
command structure with deterministic rules (see ``_CONTEXTUAL_RULES``): a dynamic assignment
prefix maps to ``assignment-prefix``; a multi-cardinality ``$@``/array-splat word maps to
``splitting-unsafe-word``; a quoted dynamic selector or subcommand under a stable ``doc-lattice``,
``uv``, or ``uvx`` head maps to ``policy-unresolvable``; a glob head maps to
``unstable-first-word``. Anything the rules do not unambiguously resolve (unquoted ``$VAR``
expansions, command
substitutions, control-flow-keyword heads, off-floor wrapper heads) keeps its conservative pin
with ``owner_adjudicate: true`` pending owner ratification. Every result is frozen in the artifact.

Run ``env -u VIRTUAL_ENV uv run --group dev python scripts/normalize_legacy_reasons.py``. The
output is deterministic: identical inputs produce byte-identical output.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT = REPO_ROOT / "tests" / "fixtures" / "github_ci_checkpoint"
SUCCESSOR = REPO_ROOT / "tests" / "fixtures" / "github_ci_successor_checkpoint"
INVENTORY = CHECKPOINT / "replay_inventory.json"
REASON_CODES = SUCCESSOR / "tables" / "reason_codes.json"
OUTPUT = SUCCESSOR / "legacy_normalization.json"

# The frozen baseline the recorded tuples are pinned to. Docs-only commits may sit on top, but
# every source file under src/ must match this commit (checked in ``_verify_baseline``).
BASELINE_COMMIT = "be4b7b16d46353ee6a38502cdc7c4ceef3487567"  # pragma: allowlist secret

# Reason categories the successor recognizer never emits, used for legacy scanner resource bounds
# (recursion, step, invocation, and nesting limits) that no successor reason code models.
LEGACY_SCAN_BUDGET = "legacy-scan-budget"
_LEGACY_ONLY_CATEGORIES: tuple[str, ...] = (LEGACY_SCAN_BUDGET,)

# Every distinct ``incomplete_reason`` string the live scanner emits over the replay inventory,
# mapped to its successor reason code (the ``code`` column of the frozen reason-code table) or to
# a legacy-only category. Grouped by the refusal the legacy string describes.
_REASON_MAP: dict[str, str] = {
    # Dynamic env/exec assignment prefixes: an assignment prefix whose value is not statically
    # known. Exact match for the command-local ``assignment-prefix`` code.
    "quoted dynamic env assignment cannot be scanned safely": "assignment-prefix",
    "unquoted dynamic env assignment cannot be scanned safely": "assignment-prefix",
    "dynamic env prefix cannot be scanned safely": "assignment-prefix",
    "expandable env prefix cannot be scanned safely": "assignment-prefix",
    # Brace or glob expansion on a command, subcommand, or launcher word: the word can expand to
    # more than one argument. Matches the S5.2 multi-cardinality-word to ``splitting-unsafe-word``
    # mapping.
    "executable word uses brace or glob expansion": "splitting-unsafe-word",
    "subcommand word uses brace or glob expansion": "splitting-unsafe-word",
    "uv command word uses brace or glob expansion": "splitting-unsafe-word",
    # Unresolved doc-lattice or uv launcher options: the source looks like a doc-lattice launch
    # but cannot be resolved under the floor. Command-local ``policy-unresolvable``.
    "unresolved doc-lattice root option": "policy-unresolvable",
    "unresolved uv launcher option": "policy-unresolvable",
    "unresolved uv global option": "policy-unresolvable",
    # Constructs the certifier does not model (locale translation, ANSI-C NUL, unsupported
    # env/exec options, and the external time(1) wrapper). Terminal ``unsupported-construct``.
    "ANSI-C quoted word decodes to NUL": "unsupported-construct",
    "locale-translated executable cannot be scanned safely": "unsupported-construct",
    "locale-translated heredoc delimiter cannot be scanned safely": "unsupported-construct",
    "unsupported env option cannot be scanned safely": "unsupported-construct",
    "unsupported exec option cannot be scanned safely": "unsupported-construct",
    "env option value cannot be scanned safely": "unsupported-construct",
    "external time option cannot be scanned safely": "unsupported-construct",
    "dynamic external time prefix cannot be scanned safely": "unsupported-construct",
    # Legacy scanner recursion bound: no successor reason code models it.
    "recursion limit exceeded": LEGACY_SCAN_BUDGET,
}

# Legacy strings that collapse several successor-distinct constructs into one bucket. Each is
# classified per entry from the source text by _classify_contextual; the value is the conservative
# pin an entry keeps (with owner_adjudicate: true) when the deterministic rules do not resolve it.
_CONTEXTUAL_PINS: dict[str, str] = {
    # The command-position expansion collapse spans a dynamic-value assignment prefix, a
    # multi-cardinality splat, a quoted dynamic selector or subcommand under a launcher head, and
    # a glob head. Conservative pin: unquoted-expansion-in-command-word.
    "command-position expansion cannot be scanned safely": "unquoted-expansion-in-command-word",
    # env -S / --split-string over off-floor env; some entries instead lead with a dynamic
    # assignment prefix, a splat head, or a glob head. Conservative pin: unsupported-construct.
    "env split-string option cannot be scanned safely": "unsupported-construct",
    # Extglob subcommand patterns; every entry is the same construct (doc-lattice PAT(...) --all),
    # so none splits. Conservative pin: unsupported-construct.
    "extglob expansion cannot be scanned safely": "unsupported-construct",
    # A dynamic relative doc-lattice path in head position (unstable first word) versus in a uv run
    # payload position (policy-unresolvable). Conservative pin: policy-unresolvable.
    "dynamic relative doc-lattice executable cannot be scanned safely": "policy-unresolvable",
}

# Successor reason categories the contextual classifier can assign, plus the conservative pins.
_CONTEXTUAL_CATEGORIES: frozenset[str] = frozenset(
    {
        "assignment-prefix",
        "splitting-unsafe-word",
        "policy-unresolvable",
        "unstable-first-word",
        "unquoted-expansion-in-command-word",
        "unsupported-construct",
    }
)

# Deterministic contextual sub-rules, recorded verbatim in the artifact so the future gate never
# re-derives a category. Applied in listed priority order; the first match wins.
_CONTEXTUAL_RULES: tuple[dict[str, str], ...] = (
    {
        "rule": "control-flow-keyword-head",
        "category": "conservative-pin",
        "spec": "A statement head that is a control-flow keyword (time, coproc, and the reserved "
        "words) refuses control-flow-keyword before the expansion under S5.2, a re-categorization "
        "left for owner ratification, so the entry keeps its conservative pin with "
        "owner_adjudicate true.",
    },
    {
        "rule": "dynamic-assignment-prefix",
        "category": "assignment-prefix",
        "spec": "A leading NAME= assignment with a dynamic value ($ in the value) refuses "
        "assignment-prefix at the earliest assignment while the argv stays resolved (S5.2 amended "
        "assignments-plus-argv-dynamic).",
    },
    {
        "rule": "multi-cardinality-splat",
        "category": "splitting-unsafe-word",
        "spec": 'A command word that is a $@, "$@", ${@...}, or array [@]/[*] splat is '
        "multi-cardinality (single=False) and refuses splitting-unsafe-word at that word (S5.2 "
        "multi-cardinality-word).",
    },
    {
        "rule": "quoted-dynamic-selector-under-launcher-head",
        "category": "policy-unresolvable",
        "spec": "A stable doc-lattice, uv, or uvx head followed by a quoted dynamic word (text "
        "unknown, single=True) passes the pre-policy precheck and refuses policy-unresolvable at "
        "the unstable selector or subcommand under launcher-policy parity (S5.2 / S6.5).",
    },
    {
        "rule": "glob-head",
        "category": "unstable-first-word",
        "spec": "A statement head carrying an unquoted glob (*, ?, [) is not statically known and "
        "refuses unstable-first-word with no string-based head logic (S5.2 first-word-unknown).",
    },
    {
        "rule": "conservative-pin",
        "category": "conservative-pin",
        "spec": "Any other shape (an unquoted $VAR expansion, a command substitution, or an "
        "off-floor wrapper head) is not unambiguously resolved by the rules above, so the entry "
        "keeps its conservative pin with owner_adjudicate true.",
    },
)

# Detection primitives for _classify_contextual, matched against the entry's raw source text.
_ASSIGN_DYNAMIC_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\+?="?\$')
_LEADING_LITERAL_ASSIGN_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*\+?=(?:'[^']*'|\"[^\"$]*\"|[^\s;$]*)\s*;\s*"
)
_SPLAT_TOKENS: tuple[str, ...] = ('"$@"', '"${@', '[@]}"', '[*]}"')
_GLOB_CHARS: frozenset[str] = frozenset("*?[")
_CONTROL_FLOW_KEYWORDS: frozenset[str] = frozenset(
    {
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "while",
        "until",
        "do",
        "done",
        "for",
        "in",
        "select",
        "case",
        "esac",
        "function",
        "!",
        "time",
        "coproc",
    }
)
_LAUNCHER_HEADS: frozenset[str] = frozenset({"doc-lattice", "uv", "uvx"})


def _statement_heads(source: str) -> list[str]:
    """Return the first word of every statement, split on ; newline && and ||."""
    heads: list[str] = []
    for part in re.split(r"[;\n]|&&|\|\|", source):
        stripped = part.strip()
        if stripped:
            heads.append(stripped.split(None, 1)[0])
    return heads


def _stable_launcher_head(source: str) -> bool:
    """Return whether the leading command head is a stable doc-lattice, uv, or uvx launcher."""
    stripped = _LEADING_LITERAL_ASSIGN_RE.sub("", source, count=1).lstrip()
    tokens = stripped.split(None, 1)
    if not tokens:
        return False
    first = tokens[0]
    if "$" in first or any(char in _GLOB_CHARS for char in first):
        return False
    return first.rsplit("/", 1)[-1] in _LAUNCHER_HEADS


def _has_glob_head(source: str) -> bool:
    """Return whether any statement head carries an unquoted glob and no expansion."""
    for head in _statement_heads(source):
        if head in ("[", "]") or "$" in head:
            continue
        if any(char in _GLOB_CHARS for char in head):
            return True
    return False


def _classify_contextual(source: str, conservative_pin: str) -> tuple[str, bool]:
    """Classify one contextual-collapse entry into a successor category and adjudication flag.

    The deterministic rules of ``_CONTEXTUAL_RULES`` are applied in priority order to the raw
    source text. A rule that resolves the entry returns its category with ``owner_adjudicate``
    False; an unresolved entry keeps ``conservative_pin`` with ``owner_adjudicate`` True.

    Args:
        source: The entry's raw execution source text.
        conservative_pin: The successor category to keep when no rule resolves the entry.

    Returns:
        The ``(reason_category, owner_adjudicate)`` pair frozen for this entry.
    """
    if any(head in _CONTROL_FLOW_KEYWORDS for head in _statement_heads(source)):
        return conservative_pin, True
    if _ASSIGN_DYNAMIC_RE.match(source):
        return "assignment-prefix", False
    if any(token in source for token in _SPLAT_TOKENS):
        return "splitting-unsafe-word", False
    if _stable_launcher_head(source) and '"$' in source:
        return "policy-unresolvable", False
    if _has_glob_head(source):
        return "unstable-first-word", False
    return conservative_pin, True


def _verify_baseline() -> None:
    """Abort unless every tracked src/ file matches the frozen baseline commit.

    Docs-only commits may sit on top of the baseline, but a source change would invalidate the
    recorded tuples, so a non-empty ``git diff --stat`` against src/ stops generation.

    Raises:
        SystemExit: If git is unavailable or src/ diverges from the baseline commit.
    """
    result = subprocess.run(
        ["git", "diff", "--stat", BASELINE_COMMIT, "--", "src/"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"baseline check failed: git diff exited {result.returncode}: {result.stderr.strip()}"
        )
    if result.stdout.strip():
        raise SystemExit(
            "src/ diverges from baseline "
            f"{BASELINE_COMMIT[:7]}; the recorded tuples would be stale:\n{result.stdout}"
        )


def _validate_mapping(valid_codes: set[str]) -> None:
    """Confirm every mapping target is a known successor code or a legacy-only category.

    Args:
        valid_codes: The ``code`` values from the frozen reason-code table.

    Raises:
        SystemExit: If a mapping target is neither a successor code nor a legacy-only category.
    """
    allowed = valid_codes | set(_LEGACY_ONLY_CATEGORIES)
    targets = set(_REASON_MAP.values()) | set(_CONTEXTUAL_PINS.values()) | _CONTEXTUAL_CATEGORIES
    unknown = sorted({category for category in targets if category not in allowed})
    if unknown:
        raise SystemExit(f"mapping targets are not valid categories: {unknown}")
    collision = sorted(set(_REASON_MAP) & set(_CONTEXTUAL_PINS))
    if collision:
        raise SystemExit(f"strings are both globally mapped and contextual: {collision}")


def _normalize_entries(entries_in: list[dict[str, object]]) -> list[dict[str, object]]:
    """Record one baseline tuple per replay entry, in inventory order.

    Args:
        entries_in: The replay-inventory entries, each carrying ``id`` and ``source``.

    Returns:
        One normalized record per entry with status, invocations, reason category, and the
        owner-adjudication flag.

    Raises:
        SystemExit: If an entry produces an ``incomplete_reason`` absent from the mapping.
    """
    from doc_lattice.github_ci.shell_scanner import (  # noqa: PLC0415
        scan_doc_lattice_invocations,
    )

    records: list[dict[str, object]] = []
    for entry in entries_in:
        source = str(entry["source"])
        result = scan_doc_lattice_invocations(source)
        invocations = [[command, dry_run] for command, dry_run in result.invocations]
        reason = result.incomplete_reason
        if reason is None:
            record: dict[str, object] = {
                "id": entry["id"],
                "status": "complete",
                "invocations": invocations,
                "reason_category": None,
                "owner_adjudicate": False,
            }
        elif reason in _CONTEXTUAL_PINS:
            category, adjudicate = _classify_contextual(source, _CONTEXTUAL_PINS[reason])
            record = {
                "id": entry["id"],
                "status": "incomplete",
                "invocations": invocations,
                "reason_category": category,
                "owner_adjudicate": adjudicate,
            }
        else:
            if reason not in _REASON_MAP:
                raise SystemExit(
                    f"unmapped incomplete_reason {reason!r} for entry {entry['id']!r}; add it to "
                    "_REASON_MAP or _CONTEXTUAL_PINS deliberately"
                )
            record = {
                "id": entry["id"],
                "status": "incomplete",
                "invocations": invocations,
                "reason_category": _REASON_MAP[reason],
                "owner_adjudicate": False,
            }
        records.append(record)
    return records


def _print_summary(entries: list[dict[str, object]]) -> None:
    """Print complete/incomplete, per-category, and adjudication counts for the operator."""
    complete = sum(1 for entry in entries if entry["status"] == "complete")
    incomplete = len(entries) - complete
    adjudicated = sum(1 for entry in entries if entry["owner_adjudicate"])
    category_counts: dict[str, int] = {}
    for entry in entries:
        category = entry["reason_category"]
        if isinstance(category, str):
            category_counts[category] = category_counts.get(category, 0) + 1
    print(f"entries: {len(entries)}  complete: {complete}  incomplete: {incomplete}")
    print(f"owner_adjudicate: {adjudicated}")
    print("reason categories:")
    for category in sorted(category_counts):
        print(f"  {category}: {category_counts[category]}")


def main() -> None:
    """Generate the legacy-reason normalization artifact and print the summary counts."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    _verify_baseline()

    reason_codes = json.loads(REASON_CODES.read_text(encoding="utf-8"))
    valid_codes = {str(row["code"]) for row in reason_codes["rows"]}
    _validate_mapping(valid_codes)

    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    entries = _normalize_entries(inventory["entries"])
    if len(entries) != inventory["count"]:
        raise SystemExit(
            f"entry count {len(entries)} does not match inventory count {inventory['count']}"
        )

    artifact = {
        "baseline_commit": BASELINE_COMMIT,
        "mapping": {reason: _REASON_MAP[reason] for reason in sorted(_REASON_MAP)},
        "contextual_mapping": {
            "note": "Four legacy strings collapse successor-distinct constructs; each entry is "
            "classified from its source text at generation, never at gate time (S6.4). Rules are "
            "applied in listed priority order, first match wins; an unresolved entry keeps its "
            "conservative pin with owner_adjudicate true.",
            "strings": {reason: _CONTEXTUAL_PINS[reason] for reason in sorted(_CONTEXTUAL_PINS)},
            "rules": list(_CONTEXTUAL_RULES),
        },
        "legacy_only_categories": sorted(_LEGACY_ONLY_CATEGORIES),
        "entries": entries,
    }
    OUTPUT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _print_summary(entries)


if __name__ == "__main__":
    main()
