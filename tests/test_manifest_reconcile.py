"""Pure orchestration of grouped sidecar-manifest reconcile updates."""

from pathlib import Path

import pytest

from doc_lattice.error_types import ManifestError
from doc_lattice.manifest_reconcile import (
    ManifestCaptureRequest,
    manifest_capture_requests,
    plan_manifest_rewrites,
    transaction_rewrite,
)
from doc_lattice.model import DocumentOrigin, ExternalDeclaration, ExternalIdentity
from doc_lattice.reconcile import ReconcileUpdate, Rewrite

_DESTINATION = Path("/project/nodes.yml")


def _identity(node_id: str, destination: Path = _DESTINATION) -> ExternalIdentity:
    declared = f"docs/{node_id}.md"
    return ExternalIdentity(declared, Path("/project") / declared, destination)


def _external_update(node_id: str, index: int, new_seen: str) -> ReconcileUpdate:
    identity = _identity(node_id)
    return ReconcileUpdate(
        new_seen,
        DocumentOrigin(
            identity.resolved_target,
            ExternalDeclaration("nodes.yml", index, identity.declared_path),
            identity,
        ),
    )


def _manifest(*records: tuple[str, tuple[tuple[str, str], ...]], ending: str = "\n") -> bytes:
    lines = ["nodes:"]
    for node_id, edges in records:
        rendered = ", ".join(f"{{ref: {ref}, seen: {seen}}}" for ref, seen in edges)
        lines.extend(
            [
                f"  - path: docs/{node_id}.md",
                f"    meta: {{id: {node_id}, derives_from: [{rendered}]}}",
            ]
        )
    return (ending.join(lines) + ending).encode()


def test_manifest_groups_produce_one_verified_result_and_inline_groups_pass_through():
    inline_destination = Path("inline.md")
    inline_group = {
        ("inline", "up"): ReconcileUpdate("new-inline", DocumentOrigin(inline_destination))
    }
    manifest_group = {("a", "up"): _external_update("a", 0, "new")}
    plan = {inline_destination: inline_group, _DESTINATION: manifest_group}
    before = _manifest(("a", (("up", "old"),)))

    inline, rewrites = plan_manifest_rewrites(
        plan,
        fresh_bytes={_DESTINATION: before},
        observations={_DESTINATION: {"a": _identity("a")}},
    )

    assert inline == {inline_destination: inline_group}
    assert inline[inline_destination] is inline_group
    assert len(rewrites) == 1
    rewrite = rewrites[0]
    assert rewrite.destination == _DESTINATION
    assert rewrite.before == before
    assert rewrite.verified_bytes == before.replace(b"seen: old", b"seen: new")
    assert [(change.node_id, change.ref, change.record_index) for change in rewrite.changed] == [
        ("a", "up", 0)
    ]


def test_fresh_record_order_and_actual_changes_drive_changed_pair_metadata():
    before = _manifest(
        ("b", (("up", "old"),)),
        ("a", (("up", "current"),)),
    )
    plan = {
        _DESTINATION: {
            ("a", "up"): _external_update("a", 0, "current"),
            ("b", "up"): _external_update("b", 1, "current"),
        }
    }

    inline, rewrites = plan_manifest_rewrites(
        plan,
        fresh_bytes={_DESTINATION: before},
        observations={_DESTINATION: {"a": _identity("a"), "b": _identity("b")}},
    )

    assert inline == {}
    assert len(rewrites) == 1
    assert rewrites[0].verified_bytes == before.replace(b"seen: old", b"seen: current")
    assert [
        (change.node_id, change.ref, change.record_index) for change in rewrites[0].changed
    ] == [("b", "up", 0)]


def test_all_current_manifest_is_omitted_even_with_mixed_line_endings():
    before = (
        b"nodes:\r\n  - path: docs/a.md\n"
        b"    meta: {id: a, derives_from: [{ref: up, seen: current}]}\r\n"
    )
    plan = {_DESTINATION: {("a", "up"): _external_update("a", 0, "current")}}

    inline, rewrites = plan_manifest_rewrites(
        plan,
        fresh_bytes={_DESTINATION: before},
        observations={_DESTINATION: {"a": _identity("a")}},
    )

    assert inline == {}
    assert rewrites == ()


@pytest.mark.parametrize(
    ("seen", "changed"),
    [
        (("shadowed", "old"), True),
        (("shadowed", "current"), False),
    ],
)
def test_last_repeated_ref_alone_decides_the_result(seen: tuple[str, str], changed: bool):
    before = _manifest(("a", (("up", seen[0]), ("up", seen[1]))))
    plan = {_DESTINATION: {("a", "up"): _external_update("a", 0, "current")}}

    _inline, rewrites = plan_manifest_rewrites(
        plan,
        fresh_bytes={_DESTINATION: before},
        observations={_DESTINATION: {"a": _identity("a")}},
    )

    if not changed:
        assert rewrites == ()
        return
    assert rewrites[0].verified_bytes == before.replace(b"seen: old", b"seen: current")
    assert [
        (change.node_id, change.ref, change.record_index) for change in rewrites[0].changed
    ] == [("a", "up", 0)]


def _mismatched_destination_update() -> ReconcileUpdate:
    identity = _identity("a", Path("/project/other.yml"))
    return ReconcileUpdate(
        "current",
        DocumentOrigin(
            identity.resolved_target, ExternalDeclaration("other.yml", 0, "docs/a.md"), identity
        ),
    )


def _conflicting_declaration_update() -> ReconcileUpdate:
    identity = _identity("b")
    return ReconcileUpdate(
        "current",
        DocumentOrigin(
            identity.resolved_target, ExternalDeclaration("./nodes.yml", 1, "docs/b.md"), identity
        ),
    )


def _conflicting_identity_update() -> ReconcileUpdate:
    return ReconcileUpdate(
        "current",
        DocumentOrigin(
            Path("/project/docs/a.md"),
            ExternalDeclaration("nodes.yml", 0, "docs/a.md"),
            ExternalIdentity("docs/a.md", Path("/project/docs/elsewhere.md"), _DESTINATION),
        ),
    )


@pytest.mark.parametrize(
    ("group", "match"),
    [
        pytest.param(
            {
                ("a", "up"): _external_update("a", 0, "current"),
                ("inline", "up"): ReconcileUpdate("current", DocumentOrigin(_DESTINATION)),
            },
            "require external identity evidence",
            id="inline-member",
        ),
        pytest.param(
            {("a", "up"): _mismatched_destination_update()},
            "does not match its resolved destination",
            id="foreign-destination",
        ),
        pytest.param(
            {
                ("a", "up"): _external_update("a", 0, "current"),
                ("b", "up"): _conflicting_declaration_update(),
            },
            "conflicting manifest declarations",
            id="two-spellings",
        ),
        pytest.param(
            {
                ("a", "one"): _external_update("a", 0, "current"),
                ("a", "two"): _conflicting_identity_update(),
            },
            "carries conflicting identities",
            id="two-identities",
        ),
    ],
)
def test_incoherent_manifest_group_is_a_caller_contract_refusal(
    group: dict[tuple[str, str], ReconcileUpdate], match: str
):
    before = _manifest(("a", (("up", "current"),)))

    with pytest.raises(ValueError, match=match):
        plan_manifest_rewrites(
            {_DESTINATION: group},
            fresh_bytes={_DESTINATION: before},
            observations={_DESTINATION: {"a": _identity("a")}},
        )


@pytest.mark.parametrize("missing", ["fresh_bytes", "observations"])
def test_manifest_group_without_a_capture_is_refused_before_rewriting(missing: str):
    before = _manifest(("a", (("up", "old"),)))
    plan = {_DESTINATION: {("a", "up"): _external_update("a", 0, "current")}}
    fresh_bytes = {} if missing == "fresh_bytes" else {_DESTINATION: before}
    observations = {} if missing == "observations" else {_DESTINATION: {"a": _identity("a")}}

    with pytest.raises(ValueError, match="lacks a fresh capture or observation"):
        plan_manifest_rewrites(plan, fresh_bytes=fresh_bytes, observations=observations)


def test_repointed_fresh_observation_refuses_the_manifest_result():
    before = _manifest(("a", (("up", "old"),)))
    plan = {_DESTINATION: {("a", "up"): _external_update("a", 0, "current")}}
    repointed = ExternalIdentity("docs/a.md", Path("/project/docs/other.md"), _DESTINATION)

    with pytest.raises(ManifestError, match="repointed"):
        plan_manifest_rewrites(
            plan,
            fresh_bytes={_DESTINATION: before},
            observations={_DESTINATION: {"a": repointed}},
        )


def test_capture_requests_name_each_manifest_by_its_declared_spelling_and_selected_ids():
    inline_destination = Path("inline.md")
    plan = {
        inline_destination: {
            ("inline", "up"): ReconcileUpdate("new-inline", DocumentOrigin(inline_destination))
        },
        _DESTINATION: {
            ("a", "up"): _external_update("a", 0, "new"),
            ("a", "other"): _external_update("a", 0, "new"),
            ("b", "up"): _external_update("b", 1, "new"),
        },
    }

    requests = manifest_capture_requests(plan)

    assert requests == {_DESTINATION: ManifestCaptureRequest("nodes.yml", frozenset({"a", "b"}))}


def test_capture_requests_refuse_an_incoherent_manifest_group():
    plan = {_DESTINATION: {("a", "up"): _mismatched_destination_update()}}

    with pytest.raises(ValueError, match="does not match its resolved destination"):
        manifest_capture_requests(plan)


def test_transaction_rewrite_publishes_exactly_the_verified_bytes():
    before = _manifest(("a", (("up", "old"), ("down", "old"))), ("b", (("up", "old"),)))
    plan = {
        _DESTINATION: {
            ("a", "up"): _external_update("a", 0, "new"),
            ("b", "up"): _external_update("b", 1, "new"),
        }
    }
    _inline, results = plan_manifest_rewrites(
        plan,
        fresh_bytes={_DESTINATION: before},
        observations={_DESTINATION: {"a": _identity("a"), "b": _identity("b")}},
    )

    rewrite = transaction_rewrite(results[0])

    assert rewrite == Rewrite(_DESTINATION, before, results[0].verified_bytes, frozenset({"up"}))
    assert rewrite.after == before.replace(b"{ref: up, seen: old}", b"{ref: up, seen: new}")
