"""Shared harness for the issue #100 recognizer evaluation gates.

Test-side only: this module orchestrates the frozen checkpoint artifacts, the old scanner
baseline, and the candidate recognizer for the predeclared gates. It never touches runtime
audit behavior.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from doc_lattice.constants import AuditSourceKind
from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci import audit as audit_module
from doc_lattice.github_ci.direct_marker_scanner import DIRECT_MARKER_RE, scan_execution_source
from doc_lattice.github_ci.model import AuditDiagnostic, BlockScan, WorkflowDocument
from doc_lattice.github_ci.reachability import job_is_pr_reachable
from doc_lattice.github_ci.shell_scanner import (
    direct_doc_lattice_invocations,
    scan_doc_lattice_invocations,
)
from doc_lattice.github_ci.workflow_parser import parse_workflow

CHECKPOINT = Path("tests/fixtures/github_ci_checkpoint")


def _load(name: str):
    """Parse one checkpoint JSON artifact."""
    return json.loads((CHECKPOINT / name).read_text())


def load_replay_inventory():
    """Return the frozen 580-entry replay inventory."""
    return _load("replay_inventory.json")


def load_tier3a_cases():
    """Return the 13 Tier 3A conformance cases."""
    return _load("tier3a_cases.json")["cases"]


def load_tier3b_provenance():
    """Return the Tier 3B provenance manifest."""
    return _load("tier3b/provenance.json")


def load_probes():
    """Return the frozen probe inventory."""
    return _load("probes.json")


def load_mutations():
    """Return the frozen boundary-mutation set."""
    return _load("mutations.json")


def load_bash_pin():
    """Return the frozen Bash pin."""
    return _load("bash_pin.json")


def tier3b_run_block(fixture_id: str) -> str:
    """Extract the single run: block of one Tier 3B workflow fixture via the real parser."""
    path = CHECKPOINT / "tier3b" / f"{fixture_id}.yml"
    document = parse_workflow(path, path.read_text())
    runs = [step.run for job in document.jobs for step in job.steps if step.run is not None]
    assert len(runs) == 1, fixture_id
    return runs[0]


@dataclass(frozen=True, slots=True)
class OldResult:
    """Normalized old-scanner outcome for one source (raw and adapter layers)."""

    certified: bool
    invocations: tuple[tuple[str, bool], ...]
    incomplete_reason: str | None
    adapter_config_error: bool


def old_scan(source: str) -> OldResult:
    """Run both old entry points and normalize their results (never exception text)."""
    result = scan_doc_lattice_invocations(source)
    try:
        direct_doc_lattice_invocations(source)
        adapter_config_error = False
    except ConfigError:
        adapter_config_error = True
    return OldResult(
        certified=result.incomplete_reason is None,
        invocations=tuple(result.invocations),
        incomplete_reason=result.incomplete_reason,
        adapter_config_error=adapter_config_error,
    )


def classify_divergence(old: OldResult, new: BlockScan) -> str:
    """Classify one old-versus-new pair into the predeclared gate 2 categories."""
    if new.status == "not_applicable":
        return "outside-direct-marker"
    if old.certified and new.status == "certified":
        return "identical" if old.invocations == new.invocations else "unexplained"
    if old.certified and new.status == "uninspectable":
        return "intentional-exit-2"
    if not old.certified and new.status == "uninspectable":
        return "identical"
    if not old.certified and new.status == "certified":
        return "old-incomplete-new-certified"
    return "unexplained"


def _tier_sources() -> list[tuple[str, str]]:
    """Return (case id, source) pairs for every tier source beyond the replay inventory."""
    sources = [(case["id"], case["source"]) for case in load_tier3a_cases()]
    for row in load_tier3b_provenance()["fixtures"]:
        sources.append((row["id"], tier3b_run_block(row["id"])))
    return sources


def replay_records() -> list[dict]:
    """Produce one normalized record per replay-inventory entry and per tier source."""
    records = []
    entries = [(entry["id"], entry["source"]) for entry in load_replay_inventory()["entries"]]
    for case_id, source in entries + _tier_sources():
        old = old_scan(source)
        new = scan_execution_source(source)
        records.append(
            {
                "id": case_id,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "old_certified": old.certified,
                "old_invocations": [list(pair) for pair in old.invocations],
                "old_incomplete_reason": old.incomplete_reason,
                "old_adapter_config_error": old.adapter_config_error,
                "new_status": new.status,
                "new_invocations": [list(pair) for pair in new.invocations],
                "new_reason_category": new.reason_category,
                "new_offset": new.offset,
                "category": classify_divergence(old, new),
            }
        )
    return records


@dataclass(frozen=True, slots=True)
class SourceEvaluation:
    """One scanned execution source and its BlockScan."""

    path: str
    job_id: str
    step_index: int
    source_kind: str
    scan: BlockScan


@dataclass(frozen=True, slots=True)
class WorkflowEvaluation:
    """D1+D6 evaluation outcome for one workflow document."""

    pruned_jobs: tuple[str, ...]
    evaluations: tuple[SourceEvaluation, ...]
    diagnostics: tuple[AuditDiagnostic, ...]


def _body_shell_class(shell: str | None, runs_on: str | None) -> str:
    """Classify a run body's effective shell per D6: BASH, NON_BASH, or UNKNOWN."""
    if shell is not None:
        # Spec-mandated reuse of the frozen D6 recognition set; SLF001 is not enabled in this
        # repo's Ruff configuration, so no noqa directive is needed (and RUF100 rejects one).
        bash = audit_module._supports_bash_run_body(shell)
        return "BASH" if bash else "NON_BASH"
    if runs_on is not None and runs_on.casefold() in audit_module._BASH_DEFAULT_RUNNERS:
        return "BASH"
    return "UNKNOWN"


def evaluate_workflow(document: WorkflowDocument) -> WorkflowEvaluation:
    """Apply D1 pruning and the D6 composition table to one workflow's PR scan."""
    path = str(document.path)
    event_names = frozenset(trigger.name for trigger in document.triggers) & audit_module.PR_EVENTS
    pruned: list[str] = []
    evaluations: list[SourceEvaluation] = []
    diagnostics: list[AuditDiagnostic] = []

    def scan_source(job_id: str, step_index: int, kind: AuditSourceKind, text: str) -> BlockScan:
        scan = scan_execution_source(text)
        evaluations.append(SourceEvaluation(path, job_id, step_index, kind, scan))
        if scan.status == "uninspectable":
            diagnostics.append(
                AuditDiagnostic(
                    path,
                    job_id,
                    step_index,
                    kind,
                    "UNINSPECTABLE_SOURCE",
                    scan.reason or "",
                    scan.offset,
                )
            )
        return scan

    def semantics_diagnostic(job_id: str, step_index: int, kind: AuditSourceKind) -> None:
        diagnostics.append(
            AuditDiagnostic(
                path,
                job_id,
                step_index,
                kind,
                "UNSUPPORTED_EXECUTION_SEMANTICS",
                "marker-bearing source executes under semantics the audit cannot inspect",
                None,
            )
        )

    for job in document.jobs:
        if not job_is_pr_reachable(job.if_condition, event_names):
            pruned.append(job.job_id)
            continue
        for step in job.steps:
            if step.run is None:
                continue
            shell = step.shell or job.default_shell or document.default_shell
            body_class = _body_shell_class(shell, job.runs_on)
            # D6's T column is judged on the author's template text: the scan sentinel that
            # replaces {0} contains the direct marker itself, so testing the substituted text
            # would misclassify every marker-free template that carries the placeholder.
            marker_in_template = bool(shell is not None and DIRECT_MARKER_RE.search(shell))
            marker_in_body = bool(DIRECT_MARKER_RE.search(step.run))
            if marker_in_template and shell is not None:
                template = shell.replace(
                    audit_module._SCRIPT_PLACEHOLDER, audit_module._SCRIPT_SENTINEL
                )
                scan_source(job.job_id, step.index, "shell_template", template)
                if body_class != "BASH":
                    semantics_diagnostic(job.job_id, step.index, "shell_template")
            if marker_in_body:
                if body_class == "BASH":
                    scan_source(job.job_id, step.index, "run_body", step.run)
                else:
                    semantics_diagnostic(job.job_id, step.index, "run_body")
    return WorkflowEvaluation(tuple(pruned), tuple(evaluations), tuple(diagnostics))
