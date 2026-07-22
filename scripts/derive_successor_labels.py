"""Re-derive successor-grammar labels for the 87-row Bash acceptance corpus (spec S8, gate 1).

The successor certifier is a real Bash parser (mvdan.cc/sh/v3) walked by the frozen
certified-construct table. This script starts from the frozen D3 acceptance labels for the
78-row prefix and from the live shell-scanner expectation for the post-#103 appendix, then
applies the spec successor deltas (S3.1, S3.2, S3.4, S5.3, S6.1, S6.2, S6.3, S6.5) row by row.

Each row's successor outcome is the judgment recorded in ``_DECISIONS``; the accompanying
``delta`` tag names the driving rule and ``derivation`` states why. The script writes the
checkpoint corpus artifact and a human-readable relabel report, and prints label and delta
counts. It is deterministic: identical inputs produce byte-identical output.

The 2026-07-21 owner ratifications from the Tier 3B predeclaration adjudication are folded in:
the ``(Assign, value)`` traverse ratification flips row 10 (assignment-only substitution) to
must-certify, and the S5.2 literal-assignment-prefix ratification (certify commands whose only
assignment prefixes carry statically known literal values) was evaluated against every row and
flips none, because no acceptance row carries a literal-assignment-prefix-plus-argv marker
command; the sole assignment refusal in the corpus is row 10, already resolved by the value
traverse.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
D3_LABELS = REPO_ROOT / "tests" / "fixtures" / "github_ci_checkpoint" / "acceptance_labels.json"
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "github_ci_successor_checkpoint" / "corpus"

MC = "must-certify"
IE = "intentional-exit-2"
OC = "outside-direct-marker-contract"

_STATUS = {MC: "certified", IE: "uninspectable", OC: "not_applicable"}

# Short invocation aliases mirroring the acceptance-row tuple shape (command, dry_run).
L = (("linear", False),)
C = (("check", False),)
R = (("reconcile", False),)
RD = (("reconcile", True),)
LL = (("linear", False), ("lint", False))
NONE: tuple[tuple[str, bool], ...] = ()


@dataclass(frozen=True)
class Decision:
    """One re-derived successor outcome for an acceptance row.

    Attributes:
        label: Successor label, one of the three frozen corpus labels.
        invocations: Retained certified invocations as (command, dry_run) pairs.
        reason_category: Successor ``ScanReasonCategory`` for uninspectable rows, else None.
        owner_adjudicate: True when the frozen contracts do not fully determine the row.
        delta: Slug naming the driving delta rule or contract stance.
        derivation: One-sentence justification recorded in the artifact.
    """

    label: str
    invocations: tuple[tuple[str, bool], ...]
    reason_category: str | None
    owner_adjudicate: bool
    delta: str
    derivation: str


# Ordered exactly like ACCEPTANCE_CASES. Rows 0-77 re-derive the frozen D3 prefix; rows 78-86
# re-derive the post-#103 live-baseline appendix.
_DECISIONS: tuple[Decision, ...] = (
    Decision(
        MC,
        L,
        None,
        False,
        "s3.3-word-text",
        "S3.3 resolves the ANSI-C-quoted head to known text doc-lattice; certified where the "
        "narrow D3 floor refused.",
    ),
    Decision(
        MC,
        L,
        None,
        True,
        "s6.2-marker-final-text",
        "S3.3 resolves the Lit and DblQuoted word parts by concatenation to the known final text "
        "doc-lattice; the marker match makes the source a candidate and certifies (flip from "
        "not_applicable).",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 IfClause condition-and-body traverses the elif condition; doc-lattice linear "
        "certifies.",
    ),
    Decision(
        MC,
        C,
        None,
        False,
        "rule1-traverse",
        "S3.2 WhileClause condition-and-body traverses the while condition; doc-lattice check "
        "certifies.",
    ),
    Decision(
        MC,
        C,
        None,
        False,
        "rule1-traverse",
        "S3.2 WhileClause traverses the until condition; doc-lattice check certifies.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-construct",
        False,
        "table-refuse",
        "S3.2 TimeClause is a refuse construct; the timed command is not reached, so the "
        "old-scanner linear is dropped fail-closed.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-construct",
        False,
        "table-refuse",
        "S3.2 CoprocClause is a refuse construct; the coproc body is not reached, so the "
        "old-scanner linear is dropped fail-closed.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 CaseItem patterns-and-body traverses the case arm; doc-lattice linear certifies.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule6-carried",
        "Carried D3 must-certify; S3.2 BinaryCmd traverses the && operands.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the command substitution in the echo argv word; doc-lattice linear "
        "certifies.",
    ),
    Decision(
        MC,
        R,
        None,
        False,
        "rule1-traverse",
        "S3.2 Assign value now traverses (owner-ratified 2026-07-21 during Task 8 Tier 3B "
        "adjudication): the command substitution in the assignment value is recursed exactly as "
        "argv, so doc-lattice reconcile certifies where the earlier frozen floor refused.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses both nested command substitutions in argv; doc-lattice linear certifies.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the command substitution inside the locale-quoted argv word (S3.3); "
        "doc-lattice linear certifies.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule1-traverse",
        "The escaped dollar is literal, so no execution source exists; echo is not a candidate "
        "and the source certifies with no invocation.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule6-carried",
        "Carried D3 must-certify; the single-quoted argument is literal data.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the outer command substitution; the inner single-quoted literal yields "
        "no doc-lattice invocation, so the source certifies empty.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the command substitution body; the comment is inert and doc-lattice "
        "linear certifies.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the command substitution body; the backticks sit inside a comment, so "
        "only printf runs and the source certifies empty.",
    ),
    Decision(
        MC,
        C,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the command substitution body; the comment line is inert and doc-lattice "
        "check certifies.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule6-carried",
        "Carried D3 must-certify; the trailing comment backslash does not continue the comment "
        "and doc-lattice linear certifies.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule6-carried",
        "Carried D3 must-certify; even trailing comment backslashes do not continue the comment.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the legacy backtick command substitution (nested); doc-lattice linear "
        "certifies.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the backtick body; the comment is inert and only printf runs, so the "
        "source certifies empty.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the backtick body; the comment is inert and doc-lattice linear certifies.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-expansion",
        False,
        "table-refuse",
        "S3.2 ParamExp is a refuse construct; the command substitution in the parameter default "
        "is not traversed, so the old-scanner linear is dropped fail-closed.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-expansion",
        False,
        "table-refuse",
        "S3.2 nested ParamExp is a refuse construct; the buried command substitution is not "
        "traversed, so the old-scanner linear is dropped fail-closed.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the command substitution body where doc-lattice linear is a direct "
        "statement; the parameter expansion argument hosts no execution source.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-expansion",
        False,
        "table-refuse",
        "S3.2 ParamExp is a refuse construct; the command substitution in the parameter default "
        "is not traversed, so the old-scanner linear is dropped fail-closed.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule6-carried",
        "Carried D3 must-certify; the single-quoted parameter-expansion text is literal data.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "The parameter expansion is a consumed argv word with no execution source; doc-lattice "
        "linear is a sibling statement that certifies.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "The parameter slice is a consumed argv word with no execution source; doc-lattice "
        "linear is a sibling statement that certifies.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 wildcard rule consumes the arithmetic expansion as an argv word-part with no "
        "execution source; doc-lattice linear is a sibling statement that certifies.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-construct",
        True,
        "scope-gap",
        "S3.2 ArithmCmd (( )) is a refuse construct; unsupported-construct is fixed terminal "
        "scope in the frozen reason-code table, but the table does not fix which code an "
        "ArithmCmd refusal emits, deciding whether the sibling doc-lattice linear is retained. "
        "Proposed fail-closed: terminal, no retained invocation.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "The legacy arithmetic expansion is a consumed argv word with no execution source; "
        "doc-lattice linear is a sibling statement that certifies.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-expansion",
        False,
        "table-refuse",
        "S3.2 ArithmExp is a refuse construct; the command substitution operand inside the "
        "arithmetic is not traversed, so the old-scanner check is dropped fail-closed.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-expansion",
        False,
        "table-refuse",
        "S3.2 ArithmExp is a refuse construct; the backtick command substitution operand is not "
        "traversed, so the old-scanner check is dropped fail-closed.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-expansion",
        False,
        "table-refuse",
        "S3.2 legacy arithmetic is a refuse construct; the command substitution operand is not "
        "traversed, so the old-scanner check is dropped fail-closed.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-expansion",
        True,
        "parse-ambiguity",
        "S3.2 Assign value is refuse and the $(( )) fallback to a command-substitution subshell "
        "is parser-behavior dependent; fail-closed intentional-exit-2 drops the old-scanner "
        "linear.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-construct",
        True,
        "parse-ambiguity",
        "Whether mvdan/sh parses (( )) as an ArithmCmd refuse or as a nested subshell traverse "
        "is parser-behavior dependent; fail-closed intentional-exit-2 drops the old-scanner "
        "linear.",
    ),
    Decision(
        OC,
        NONE,
        None,
        False,
        "rule6-carried",
        "Carried D3 not_applicable; the source carries no marker in raw or final text.",
    ),
    Decision(
        OC,
        NONE,
        None,
        False,
        "rule6-carried",
        "Carried D3 not_applicable; the balanced arithmetic carries no marker.",
    ),
    Decision(
        OC,
        NONE,
        None,
        False,
        "rule6-carried",
        "Carried D3 not_applicable; the arithmetic assignment carries no marker.",
    ),
    Decision(
        OC,
        NONE,
        None,
        False,
        "rule6-carried",
        "Carried D3 not_applicable; the nested balanced arithmetic carries no marker.",
    ),
    Decision(
        OC,
        NONE,
        None,
        False,
        "rule6-carried",
        "Carried D3 not_applicable; the unterminated arithmetic carries no marker, so it is "
        "never batched.",
    ),
    Decision(
        MC,
        C,
        None,
        False,
        "rule1-traverse",
        "The unquoted heredoc body is literal data with no execution source; the sibling "
        "statement doc-lattice check certifies.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the command substitution in the unquoted-heredoc body; doc-lattice "
        "linear certifies.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "The unquoted-heredoc body treats quotes as literal; S3.2 traverses the command "
        "substitution and doc-lattice linear certifies.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule1-traverse",
        "The escaped dollar in the unquoted-heredoc body is literal; cat is not a candidate and "
        "the source certifies empty.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule1-traverse",
        "S3.2 treats the quoted-heredoc body as ignored data; the substitution is suppressed and "
        "the source certifies empty.",
    ),
    Decision(
        IE,
        NONE,
        "parser-divergence-guard",
        True,
        "rule5-heredoc-guard",
        "S3.4 flags the backslash-newline continuation in the heredoc delimiter word; whether "
        "the guard fires is parser-behavior dependent. Proposed fail-closed: guard refusal.",
    ),
    Decision(
        IE,
        NONE,
        "parser-divergence-guard",
        True,
        "rule5-heredoc-guard",
        "S3.4 flags the backslash-newline continuation in the double-quoted delimiter word; "
        "parser-behavior dependent. Proposed fail-closed: guard refusal.",
    ),
    Decision(
        IE,
        NONE,
        "parser-divergence-guard",
        True,
        "rule5-heredoc-guard",
        "S3.4 flags the backslash-newline continuation in the single-quoted delimiter word; "
        "parser-behavior dependent. Proposed fail-closed: guard refusal.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule1-traverse",
        "The ANSI-C-quoted heredoc delimiter suppresses expansion (quoted-heredoc body ignored); "
        "the source certifies empty.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the backtick command substitution in the unquoted-heredoc body; "
        "doc-lattice linear certifies.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule1-traverse",
        "S3.2 ignores the quoted-heredoc body; the backticks are suppressed and the source "
        "certifies empty.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "The hash is literal in the unquoted-heredoc body; S3.2 traverses the command "
        "substitution and doc-lattice linear certifies.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the outer command substitution and the inner unquoted-heredoc command "
        "substitution; doc-lattice linear certifies.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the outer command substitution; the inner quoted heredoc is ignored data "
        "and the source certifies empty.",
    ),
    Decision(
        MC,
        LL,
        None,
        False,
        "rule1-traverse",
        "Heredoc A unquoted traverses to doc-lattice linear; heredoc B quoted suppresses "
        "reconcile; the sibling doc-lattice lint certifies, matching the old-scanner tuple.",
    ),
    Decision(
        IE,
        NONE,
        "parser-divergence-guard",
        False,
        "rule5-heredoc-guard",
        "S3.4 canonical body backslash-newline false-safe (documented permanent regression); the "
        "continuation suppresses the physical delimiter, so the guard fires terminally with no "
        "invocation.",
    ),
    Decision(
        IE,
        NONE,
        "parser-divergence-guard",
        True,
        "rule5-heredoc-guard",
        "S3.4 body backslash-newline continuation that forms the delimiter; guard-versus-certify "
        "is parser-behavior dependent. Proposed fail-closed: guard refusal, dropping old linear.",
    ),
    Decision(
        IE,
        NONE,
        "parser-divergence-guard",
        True,
        "rule5-heredoc-guard",
        "S3.4 body backslash-newline continuation that forms a command substitution; "
        "guard-versus-certify is parser-behavior dependent. Proposed fail-closed: guard refusal, "
        "dropping old linear.",
    ),
    Decision(
        MC,
        L,
        None,
        True,
        "rule1-traverse",
        "S3.2 traverses the here-string word as a redirect target-word expansion; the command "
        "substitution executes and doc-lattice linear certifies. Proposal: here-string word "
        "mapped to Redirect target-word-expansion traverse; frozen table lacks an explicit "
        "here-string row; ratify the mapping or add the row.",
    ),
    Decision(
        MC,
        NONE,
        None,
        True,
        "rule1-traverse",
        "The here-string word is literal; cat is not a candidate and the source certifies empty. "
        "Proposal: here-string word mapped to Redirect target-word-expansion traverse; frozen "
        "table lacks an explicit here-string row; ratify the mapping or add the row.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-construct",
        False,
        "table-refuse",
        "S3.2 ProcSubst is a refuse construct; the input process substitution is not traversed, "
        "so the old-scanner linear is dropped fail-closed.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-construct",
        False,
        "table-refuse",
        "S3.2 ProcSubst is a refuse construct; the output process substitution is not traversed, "
        "so the old-scanner linear is dropped fail-closed.",
    ),
    Decision(
        IE,
        NONE,
        "unsupported-construct",
        False,
        "table-refuse",
        "S3.2 ProcSubst is a refuse construct; the process substitution is not traversed and the "
        "source refuses.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "The named-fd redirect target is inert; the command doc-lattice linear certifies.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "The redirect between head and argument is inert; doc-lattice linear certifies.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "rule1-traverse",
        "The redirect after the uv run launcher is inert; doc-lattice linear certifies.",
    ),
    Decision(
        MC,
        R,
        None,
        False,
        "rule1-traverse",
        "The --dry-run word is the redirect target filename, not an argv token; reconcile "
        "certifies without dry-run.",
    ),
    Decision(
        MC,
        R,
        None,
        True,
        "rule1-traverse",
        "The --dry-run word is the here-string redirect word (target-word expansion), not an "
        "argv token; reconcile certifies without dry-run. Proposal: here-string word mapped to "
        "Redirect target-word-expansion traverse; frozen table lacks an explicit here-string "
        "row; ratify the mapping or add the row.",
    ),
    Decision(
        MC,
        RD,
        None,
        False,
        "rule6-carried",
        "Carried D3 must-certify; the quoted --dry-run stays an argv token.",
    ),
    Decision(
        MC,
        R,
        None,
        False,
        "rule6-carried",
        "Carried D3 must-certify; the dynamic value word cannot supply a distinct --dry-run "
        "token, so reconcile certifies without dry-run.",
    ),
    Decision(
        MC,
        C,
        None,
        False,
        "rule1-traverse",
        "S3.2 traverses the redirect target-word expansion; the command substitution executes "
        "and doc-lattice check certifies.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule1-traverse",
        "The multiline double-quoted literal parses cleanly; printf is not a candidate and the "
        "source certifies empty (the D3 quote-spans-newline category is retired).",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "rule1-traverse",
        "The multiline single-quoted literal parses cleanly; printf is not a candidate and the "
        "source certifies empty (the D3 quote-spans-newline category is retired).",
    ),
    Decision(
        IE,
        C,
        "syntax-error",
        False,
        "rule4-malformed-tail",
        "S3.1 and S5.3 retain doc-lattice check from the completed first statement before the "
        "terminal syntax-error at the unterminated command substitution.",
    ),
    Decision(
        IE,
        NONE,
        "policy-unresolvable",
        False,
        "s6.5-launcher-parity",
        "Live baseline is incomplete; S6.5 preserves the launcher-policy fail-closed refusal of "
        "the uv tool short option before the selector.",
    ),
    Decision(
        IE,
        NONE,
        "policy-unresolvable",
        False,
        "s6.5-launcher-parity",
        "Live baseline is incomplete; S6.5 preserves the fail-closed refusal of the uv tool long "
        "option before the selector.",
    ),
    Decision(
        IE,
        NONE,
        "policy-unresolvable",
        False,
        "s6.5-launcher-parity",
        "Live baseline is incomplete; S6.5 preserves the fail-closed refusal of the uv tool "
        "value option before the selector.",
    ),
    Decision(
        IE,
        NONE,
        "policy-unresolvable",
        False,
        "s6.5-launcher-parity",
        "Live baseline is incomplete; S6.5 preserves the fail-closed refusal of the uv tool "
        "option before a non-run selector.",
    ),
    Decision(
        IE,
        NONE,
        "policy-unresolvable",
        False,
        "s6.5-launcher-parity",
        "Live baseline is incomplete; S6.5 preserves the fail-closed refusal of the uv tool "
        "dynamic value option before the selector.",
    ),
    Decision(
        MC,
        NONE,
        None,
        False,
        "s6.5-launcher-parity",
        "Live baseline is empty; uv tool install is not a doc-lattice launch, so the source "
        "certifies with no invocation.",
    ),
    Decision(
        IE,
        NONE,
        "policy-unresolvable",
        False,
        "s6.5-launcher-parity",
        "Live baseline is incomplete; S6.5 preserves the fail-closed refusal of uvx --no-sync.",
    ),
    Decision(
        IE,
        NONE,
        "policy-unresolvable",
        False,
        "s6.5-launcher-parity",
        "Live baseline is incomplete; S6.5 preserves the fail-closed refusal of uv tool run "
        "--no-sync.",
    ),
    Decision(
        MC,
        L,
        None,
        False,
        "s6.5-launcher-parity",
        "Live baseline certifies; uv run --no-sync remains a certified doc-lattice linear launch.",
    ),
)


def _load_acceptance() -> tuple[list[tuple[str, str, object]], object]:
    """Load the acceptance rows and the INCOMPLETE sentinel from the sibling test module."""
    if str(TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(TESTS_DIR))
    from test_github_ci_shell_scanner import (  # noqa: PLC0415  # ty: ignore[unresolved-import]
        ACCEPTANCE_CASES,
        INCOMPLETE,
    )

    return list(ACCEPTANCE_CASES), INCOMPLETE


def _baseline(
    index: int,
    expected: object,
    is_incomplete: bool,
    d3: list[dict[str, object]],
) -> tuple[str, str, str | None]:
    """Return the baseline label, invocation string, and reason category for the report."""
    if index < len(d3):
        entry = d3[index]
        inv = entry.get("expected_invocations", [])
        reason = entry.get("reason_category")
        return str(entry["label"]), json.dumps(inv), (str(reason) if reason is not None else None)
    if is_incomplete:
        return "live-incomplete", "[]", None
    assert isinstance(expected, tuple)
    pairs = [list(pair) for pair in expected if isinstance(pair, tuple)]
    return "live-certified", json.dumps(pairs), None


def _case_dict(description: str, source: str, decision: Decision) -> dict[str, object]:
    """Serialize one re-derived decision to the frozen corpus artifact shape."""
    case: dict[str, object] = {
        "description": description,
        "source": source,
        "label": decision.label,
        "expected_status": _STATUS[decision.label],
        "expected_invocations": [list(pair) for pair in decision.invocations],
    }
    if decision.reason_category is not None:
        case["reason_category"] = decision.reason_category
    case["derivation"] = decision.derivation
    case["owner_adjudicate"] = decision.owner_adjudicate
    return case


def _render_report(rows: list[tuple[int, str, Decision, tuple[str, str, str | None]]]) -> str:
    """Render the relabel report: summary counts, changed rows, and adjudication list."""
    label_counts: dict[str, int] = {MC: 0, IE: 0, OC: 0}
    delta_counts: dict[str, int] = {}
    for _index, _desc, decision, _base in rows:
        label_counts[decision.label] += 1
        delta_counts[decision.delta] = delta_counts.get(decision.delta, 0) + 1

    lines: list[str] = []
    lines.append("# Successor relabel report for the acceptance corpus")
    lines.append("")
    lines.append(
        "Re-derivation of every acceptance row against the frozen successor tables (S8, gate 1)."
    )
    lines.append("Deterministic output; see `scripts/derive_successor_labels.py`.")
    lines.append("")
    lines.append("## Label counts")
    lines.append("")
    for label in (MC, IE, OC):
        lines.append(f"- {label}: {label_counts[label]}")
    lines.append("")
    lines.append("## Applied-delta counts")
    lines.append("")
    for delta in sorted(delta_counts):
        lines.append(f"- {delta}: {delta_counts[delta]}")
    lines.append("")

    changed = [
        (i, desc, dec, base)
        for (i, desc, dec, base) in rows
        if (dec.label, json.dumps([list(p) for p in dec.invocations]), dec.reason_category) != base
    ]
    lines.append(f"## Rows changed versus D3/live baseline ({len(changed)})")
    lines.append("")
    lines.append("| # | description | baseline | successor | invocations | delta |")
    lines.append("| - | ----------- | -------- | --------- | ----------- | ----- |")
    for i, desc, dec, base in changed:
        inv = json.dumps([list(p) for p in dec.invocations])
        lines.append(f"| {i} | {desc} | {base[0]} | {dec.label} | {inv} | {dec.delta} |")
    lines.append("")

    adjudicated = [(i, desc, dec) for (i, desc, dec, _base) in rows if dec.owner_adjudicate]
    lines.append(f"## Owner-adjudicate rows ({len(adjudicated)})")
    lines.append("")
    lines.append("These rows are not fully determined by the frozen contracts and carry a")
    lines.append("fail-closed proposal for Rick's checkpoint review.")
    lines.append("")
    for i, desc, dec in adjudicated:
        lines.append(f"- Row {i} ({desc}): proposed {dec.label}. {dec.derivation}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    """Derive the corpus artifact and relabel report, and print the summary counts."""
    acceptance, incomplete = _load_acceptance()
    if len(acceptance) != len(_DECISIONS):
        raise SystemExit(
            f"row-count mismatch: {len(acceptance)} acceptance rows, {len(_DECISIONS)} decisions"
        )
    d3 = json.loads(D3_LABELS.read_text(encoding="utf-8"))["cases"]

    cases: list[dict[str, object]] = []
    rows: list[tuple[int, str, Decision, tuple[str, str, str | None]]] = []
    for index, ((description, script, expected), decision) in enumerate(
        zip(acceptance, _DECISIONS, strict=True)
    ):
        cases.append(_case_dict(description, script, decision))
        base = _baseline(index, expected, expected is incomplete, d3)
        rows.append((index, description, decision, base))

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    artifact = json.dumps({"cases": cases}, ensure_ascii=False, indent=2)
    (CORPUS_DIR / "acceptance_labels.json").write_text(artifact + "\n", encoding="utf-8")
    (CORPUS_DIR / "relabel_report.md").write_text(_render_report(rows), encoding="utf-8")

    label_counts = {MC: 0, IE: 0, OC: 0}
    delta_counts: dict[str, int] = {}
    adjudicate = 0
    for _i, _d, decision, _b in rows:
        label_counts[decision.label] += 1
        delta_counts[decision.delta] = delta_counts.get(decision.delta, 0) + 1
        adjudicate += int(decision.owner_adjudicate)
    print("label counts:")
    for label in (MC, IE, OC):
        print(f"  {label}: {label_counts[label]}")
    print("delta counts:")
    for delta in sorted(delta_counts):
        print(f"  {delta}: {delta_counts[delta]}")
    print(f"owner_adjudicate: {adjudicate}")


if __name__ == "__main__":
    main()
