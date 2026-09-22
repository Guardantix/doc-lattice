"""Pure sidecar rewrite behavior and byte preservation."""

from pathlib import Path

import pytest

from doc_lattice import sidecar_rewrite
from doc_lattice.error_types import ManifestError
from doc_lattice.model import ExternalIdentity


def _identity(node: str, path: str | None = None) -> ExternalIdentity:
    declared = path or f"docs/{node}.md"
    return ExternalIdentity(declared, Path("/project") / declared, Path("/project/nodes.yml"))


def _rewrite(
    source: bytes,
    updates: dict[tuple[str, str], str],
    *,
    expected: dict[str, ExternalIdentity] | None = None,
    observed: dict[str, ExternalIdentity] | None = None,
) -> bytes:
    ids = {node for node, _ in updates}
    expected = expected if expected is not None else {node: _identity(node) for node in ids}
    observed = observed if observed is not None else dict(expected)
    return sidecar_rewrite.rewrite_manifest_bytes(
        source,
        source="nodes.yml",
        expected=expected,
        observed=observed,
        updates=updates,
    )


def test_only_selected_record_changes_when_two_records_share_a_ref():
    before = (
        b"nodes:\n"
        b"  - path: docs/a.md\n    meta: {id: a, derives_from: [{ref: up, seen: old}]}\n"
        b"  - path: docs/b.md\n    meta: {id: b, derives_from: [{ref: up, seen: old}]}\n"
    )
    after = (
        b"nodes:\n"
        b"  - path: docs/a.md\n    meta: {id: a, derives_from: [{ref: up, seen: new}]}\n"
        b"  - path: docs/b.md\n    meta: {id: b, derives_from: [{ref: up, seen: old}]}\n"
    )
    assert _rewrite(before, {("a", "up"): "new"}) == after


def test_multiple_selected_records_produce_one_after_image_despite_reorder():
    before = (
        b"# retain\nnodes:\n"
        b"  - path: docs/b.md\n    meta: {id: b, derives_from: [{ref: up, seen: old}]}\n"
        b"  - path: docs/a.md\n    meta: {id: a, derives_from: [{ref: up, seen: old}]}\n"
    )
    after = before.replace(b"seen: old", b"seen: new")
    assert _rewrite(before, {("a", "up"): "new", ("b", "up"): "new"}) == after


def test_two_refs_in_one_record_share_one_verified_after_image():
    before = (
        b"nodes:\n  - path: docs/a.md\n    meta:\n      id: a\n      derives_from:\n"
        b"        - {ref: one, seen: old1}\n        - {ref: two, seen: old2}\n"
    )
    after = before.replace(b"old1", b"new1").replace(b"old2", b"new2")
    assert _rewrite(before, {("a", "one"): "new1", ("a", "two"): "new2"}) == after


@pytest.mark.parametrize(
    ("seens", "expected"),
    [
        ("old, current", "old, current"),
        ("current, old", "current, current"),
    ],
)
def test_repeated_ref_updates_only_last_effective_occurrence(seens: str, expected: str):
    first, second = seens.split(", ")
    after_first, after_second = expected.split(", ")
    before = (
        "nodes:\n  - path: docs/a.md\n    meta:\n      id: a\n      derives_from:\n"
        f"        - {{ref: up, seen: {first}}}\n"
        f"        - {{ref: up, seen: {second}}}\n"
    ).encode()
    after = (
        "nodes:\n  - path: docs/a.md\n    meta:\n      id: a\n      derives_from:\n"
        f"        - {{ref: up, seen: {after_first}}}\n"
        f"        - {{ref: up, seen: {after_second}}}\n"
    ).encode()
    assert _rewrite(before, {("a", "up"): "current"}) == after


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
def test_uniform_line_endings_and_comments_are_preserved(ending: bytes):
    before = ending.join(
        [
            b"nodes:",
            b"  - path: docs/a.md # keep path comment",
            b"    meta:",
            b"      id: a",
            b"      derives_from:",
            b"        - ref: up",
            b"          seen: old # keep scalar comment",
            b"  - path: docs/b.md",
            b"    meta: {id: b}",
            b"",
        ]
    )
    after = before.replace(b"seen: old", b"seen: new")
    assert _rewrite(before, {("a", "up"): "new"}) == after


def test_mixed_endings_refuse_an_actual_rewrite_but_allow_a_noop():
    before = (
        b"nodes:\r\n  - path: docs/a.md\n"
        b"    meta: {id: a, derives_from: [{ref: up, seen: old}]}\r\n"
    )
    assert _rewrite(before, {("a", "up"): "old"}) == before
    with pytest.raises(ManifestError, match="mixed line endings"):
        _rewrite(before, {("a", "up"): "new"})


def test_refusal_escapes_the_manifest_path_for_display():
    before = (
        b"nodes:\r\n  - path: docs/a.md\n"
        b"    meta: {id: a, derives_from: [{ref: up, seen: old}]}\r\n"
    )
    with pytest.raises(ManifestError) as caught:
        sidecar_rewrite.rewrite_manifest_bytes(
            before,
            source="bad\nname.yml",
            expected={"a": _identity("a")},
            observed={"a": _identity("a")},
            updates={("a", "up"): "new"},
        )
    assert "bad\\nname.yml" in str(caught.value)
    assert "bad\nname.yml" not in str(caught.value)


def test_directive_and_flow_manifest_preserve_source_around_missing_seen():
    before = (
        b"%YAML 1.2\n---\n"
        b"nodes: [{path: docs/a.md, meta: {id: a, derives_from: [{ref: up}]}}] # keep\n"
    )
    after = (
        b"%YAML 1.2\n---\n"
        b"nodes: [{path: docs/a.md, meta: {id: a, derives_from: [{ref: up, seen: new}]}}] # keep\n"
    )
    assert _rewrite(before, {("a", "up"): "new"}) == after


def test_explicit_null_seen_changes_only_its_value():
    before = (
        b"nodes:\n  - path: docs/a.md\n    meta:\n      id: a\n"
        b"      derives_from:\n        - ref: up\n          seen: null # keep\n"
    )
    after = before.replace(b"seen: null", b"seen: new")
    assert _rewrite(before, {("a", "up"): "new"}) == after


def test_quoted_seen_replaces_only_the_scalar_source():
    before = b"nodes: [{path: docs/a.md, meta: {id: a, derives_from: [{ref: up, seen: 'old'}]}}]\n"
    after = before.replace(b"seen: 'old'", b"seen: new")
    assert _rewrite(before, {("a", "up"): "new"}) == after


@pytest.mark.parametrize(
    ("before", "message"),
    [
        (b"nodes:\n  - path: docs/b.md\n    meta: {id: b}\n", "missing"),
        (
            b"nodes:\n  - path: docs/a.md\n    meta: {id: a, derives_from: [{ref: up}]}\n"
            b"  - path: docs/a.md\n    meta: {id: a, derives_from: [{ref: up}]}\n",
            "duplicated",
        ),
        (
            b"nodes:\n  - path: ./docs/a.md\n    meta: {id: a, derives_from: [{ref: up}]}\n",
            "repointed",
        ),
    ],
)
def test_missing_duplicated_or_repointed_selected_record_refuses(before: bytes, message: str):
    with pytest.raises(ManifestError, match=message):
        _rewrite(before, {("a", "up"): "new"})


def test_changed_resolved_target_or_manifest_refuses_even_when_bytes_match():
    before = b"nodes:\n  - path: docs/a.md\n    meta: {id: a, derives_from: [{ref: up}]}\n"
    expected = {"a": _identity("a")}
    for observed in (
        {
            "a": ExternalIdentity(
                "docs/a.md", Path("/project/other.md"), expected["a"].resolved_manifest
            )
        },
        {
            "a": ExternalIdentity(
                "docs/a.md", expected["a"].resolved_target, Path("/project/other.yml")
            )
        },
    ):
        with pytest.raises(ManifestError, match="repointed"):
            _rewrite(before, {("a", "up"): "new"}, expected=expected, observed=observed)


@pytest.mark.parametrize("missing", ["expected", "observed"])
def test_missing_identity_evidence_refuses(missing: str):
    before = b"nodes:\n  - path: docs/a.md\n    meta: {id: a, derives_from: [{ref: up}]}\n"
    expected = {} if missing == "expected" else {"a": _identity("a")}
    observed = {} if missing == "observed" else {"a": _identity("a")}
    with pytest.raises(ManifestError, match="identity evidence"):
        _rewrite(before, {("a", "up"): "new"}, expected=expected, observed=observed)


def test_refusals_are_ordered_by_node_id_across_failure_kinds():
    """An earlier id lacking evidence is named before a later id the manifest lacks."""
    before = b"nodes:\n  - path: docs/a.md\n    meta: {id: a, derives_from: [{ref: up}]}\n"
    updates = {("a", "up"): "new", ("z", "up"): "new"}
    with pytest.raises(ManifestError, match="'a' lacks identity evidence"):
        _rewrite(before, updates, expected={}, observed={})


@pytest.mark.parametrize("corruption", ["unselected", "order"])
def test_whole_after_image_rejects_an_unselected_change_or_reorder(
    monkeypatch: pytest.MonkeyPatch, corruption: str
):
    before = (
        b"nodes:\n"
        b"  - path: docs/a.md\n    meta: {id: a, derives_from: [{ref: up, seen: old}]}\n"
        b"  - path: docs/b.md\n    meta: {id: b}\n"
    )
    original = sidecar_rewrite.source_edit._apply_source_edits

    def corrupt(text, edits):
        rewritten = original(text, edits)
        if corruption == "unselected":
            return rewritten.replace("id: b", "id: c")
        first, second = rewritten.split("  - path:")[1:]
        return "nodes:\n  - path:" + second + "  - path:" + first

    monkeypatch.setattr(sidecar_rewrite.source_edit, "_apply_source_edits", corrupt)
    with pytest.raises(ManifestError, match="after-bytes"):
        _rewrite(before, {("a", "up"): "new"})
