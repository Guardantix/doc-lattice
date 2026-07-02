"""Pure join: grade a trigger node's tickets into ordered stale-shipped findings."""

from collections.abc import Mapping

from .check import check_lattice
from .constants import BlockedReason, Severity
from .impact import expand_targets, impact
from .model import Lattice, TargetId
from .tickets import Finding, Ticket

_STATE_SEVERITY: dict[str, Severity] = {
    "completed": "DANGER",
    "started": "WARNING",
    "unstarted": "INFO",
    "backlog": "INFO",
}
_SEVERITY_RANK: dict[Severity, int] = {"DANGER": 0, "BLOCKED": 1, "WARNING": 2, "INFO": 3}


def build_audit_trigger(lattice: Lattice, target: str | None) -> dict[str, tuple[str, ...]]:
    """Map each currently-STALE node to its stale upstream refs.

    Args:
        lattice: The built lattice.
        target: An optional id; when given, the trigger is narrowed to STALE nodes that are
            ``target`` itself or fall in its impact set, so scoping the audit to a node still
            grades that node's own shipped tickets, not only its dependents'.

    Returns:
        A map of downstream node id to the tuple of its STALE ``target_ref`` values.

    Raises:
        ValidationError: If ``target`` is given but resolves to no id.
    """
    grouped: dict[str, list[str]] = {}
    for status in check_lattice(lattice):
        if status.state == "STALE":
            grouped.setdefault(status.source_id, []).append(status.target_ref)
    trigger = {node_id: tuple(refs) for node_id, refs in grouped.items()}
    if target is not None:
        affected = {node.id for node in impact(lattice, target)}
        # impact() returns strict dependents and excludes target itself; add target's own
        # node so a CI gate scoped to a node cannot pass while that node ships a stale ticket.
        # expand_targets yields TargetIds; bridge whole-file targets back to node ids so the
        # target's own node is added to `affected`. Without this the filter is always-False
        # (a TargetId is never a str key) and a scoped audit drops the target's own tickets.
        affected |= {
            tid.file_id
            for tid in expand_targets(lattice, target)
            if tid.anchor is None and tid.file_id in lattice.nodes_by_id
        }
        trigger = {node_id: refs for node_id, refs in trigger.items() if node_id in affected}
    return trigger


def build_from_trigger(lattice: Lattice, from_id: str) -> dict[str, tuple[str, ...]]:
    """Map each node downstream of ``from_id`` to the refs that connect it to the change.

    Args:
        lattice: The built lattice.
        from_id: The id about to change.

    Returns:
        A map of affected node id to the tuple of its ``target_ref`` values whose resolved
        target lies in the transitive impacted-id closure of ``from_id``.

    Raises:
        ValidationError: If ``from_id`` resolves to no id.
    """
    affected = impact(lattice, from_id)
    closure: set[TargetId] = set(expand_targets(lattice, from_id))
    for node in affected:
        # An affected node's whole file is in the closure; use its file target, not the bare
        # node id, so an edge deriving from the whole file matches `edge.target_id in closure`.
        closure.add(TargetId(node.id))
        # An affected node's path is a tracked node, so it keys anchors_by_path by
        # construction; index direct to fail loud on an incoherent index.
        closure |= lattice.anchors_by_path[node.path]
    trigger: dict[str, tuple[str, ...]] = {}
    for node in affected:
        refs = tuple(edge.target_ref for edge in node.derives_from if edge.target_id in closure)
        if refs:
            trigger[node.id] = refs
    return trigger


def stale_shipped(
    lattice: Lattice,
    trigger: Mapping[str, tuple[str, ...]],
    tickets: Mapping[str, Ticket],
    rejected: Mapping[str, BlockedReason],
) -> list[Finding]:
    """Grade each trigger node's tickets into deterministically ordered findings.

    Args:
        lattice: The built lattice.
        trigger: Node id to its justifying drifted refs.
        tickets: Resolved tickets keyed by queried identifier.
        rejected: Refs refused before fetch, mapped to ``"malformed"`` or ``"cross-team"``.

    Returns:
        Findings ordered by severity rank (DANGER, BLOCKED, WARNING, INFO), then node id,
        then ticket ref. A node with no tickets, or only terminal-state tickets, yields none.
    """
    findings: list[Finding] = []
    for node_id, drifted_refs in trigger.items():
        node = lattice.nodes_by_id[node_id]
        for ref in dict.fromkeys(node.tickets):
            ticket = tickets.get(ref)
            severity: Severity
            reason: BlockedReason | None
            if ticket is not None:
                graded = _STATE_SEVERITY.get(ticket.state.type)
                if graded is None:
                    continue
                severity = graded
                reason = None
            else:
                severity = "BLOCKED"
                reason = rejected.get(ref, "not-found")
            findings.append(
                Finding(
                    severity=severity,
                    node_id=node_id,
                    node_title=node.title,
                    node_path=node.path,
                    drifted_refs=drifted_refs,
                    ticket_ref=ref,
                    reason=reason,
                    ticket=ticket,
                )
            )
    findings.sort(key=lambda f: (_SEVERITY_RANK[f.severity], f.node_id, f.ticket_ref))
    return findings
