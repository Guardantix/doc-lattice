"""Tests for reconcile."""

from pathlib import Path

import pytest
from ruamel.yaml import YAML
from ruamel.yaml.parser import Parser

from doc_lattice import reconcile as reconcile_module
from doc_lattice.check import check_lattice
from doc_lattice.config import load_config
from doc_lattice.error_types import (
    BrokenRefError,
    ProjectError,
    UnreadableDocError,
    ValidationError,
)
from doc_lattice.frontmatter_parser import parse_meta, split_frontmatter
from doc_lattice.hashing import content_hash
from doc_lattice.loader import build_lattice
from doc_lattice.model import NodeMeta, ParsedDoc, RawEdge, TargetId, parse_ref
from doc_lattice.orchestrate import load_lattice
from doc_lattice.reconcile import apply_reconcile, plan_rewrites, reconcile


def _apply_plan(plan: dict[Path, dict[str, str]]) -> None:
    for path, updates in plan.items():
        new_text, _ = apply_reconcile(path.read_text(encoding="utf-8"), updates, path)
        path.write_text(new_text, encoding="utf-8")


def _planned_refs(plan: dict[Path, dict[str, str]]) -> set[str]:
    """Collect every target ref across all files in a reconcile plan."""
    return {ref for updates in plan.values() for ref in updates}


def _reloaded_tickets(text: str) -> list[object]:
    """Return reconciled `tickets` as the safe loader sees them, element types included."""
    raw_meta, _ = split_frontmatter(text, Path("downstream.md"))
    reloaded = YAML(typ="safe").load(raw_meta)
    assert isinstance(reloaded, dict)
    tickets = reloaded["tickets"]
    assert isinstance(tickets, list)
    return tickets


def _validated_reconcile_meta(text: str) -> NodeMeta:
    """Reparse reconciled text through the normal typed frontmatter boundary."""
    raw_meta, _ = split_frontmatter(text, Path("downstream.md"))
    meta = parse_meta(raw_meta, Path("downstream.md"))
    assert meta is not None
    return meta


def test_plan_rewrites_applies_updates_from_reader():
    path = Path("downstream.md")
    source = (
        "---\r\nid: d\r\nderives_from:\r\n  - ref: a#x\r\n    seen: old\r\n---\r\ncafé ☕\r\n"
    ).encode()
    expected_after = (
        "---\r\nid: d\r\nderives_from:\r\n  - ref: a#x\r\n    seen: newhash\r\n---\r\ncafé ☕\r\n"
    ).encode()

    rewrites = plan_rewrites({path: {"a#x": "newhash"}}, lambda _path: source)

    assert len(rewrites) == 1
    rewrite = rewrites[0]
    assert rewrite.path == path
    assert rewrite.before == source
    assert isinstance(rewrite.after, bytes)
    assert rewrite.after == expected_after
    assert isinstance(rewrite.applied, frozenset)
    assert rewrite.applied == frozenset({"a#x"})


def test_plan_rewrites_restores_lone_cr_line_endings():
    path = Path("downstream.md")
    source = b"---\rid: d\rderives_from:\r  - ref: a#x\r    seen: old\r---\rbody\r"
    expected_after = b"---\rid: d\rderives_from:\r  - ref: a#x\r    seen: newhash\r---\rbody\r"

    rewrites = plan_rewrites({path: {"a#x": "newhash"}}, lambda _path: source)

    assert len(rewrites) == 1
    assert rewrites[0].before == source
    assert rewrites[0].after == expected_after


def test_plan_rewrites_normalizes_a_file_that_mixes_line_endings():
    # A mixed file has no single ending to restore, so the spliced LF text is what ships.
    path = Path("downstream.md")
    source = b"---\nid: d\r\nderives_from:\n  - ref: a#x\r\n    seen: old\n---\nbody\n"
    expected_after = b"---\nid: d\nderives_from:\n  - ref: a#x\n    seen: newhash\n---\nbody\n"

    rewrites = plan_rewrites({path: {"a#x": "newhash"}}, lambda _path: source)

    assert len(rewrites) == 1
    assert rewrites[0].before == source
    assert rewrites[0].after == expected_after


def test_plan_rewrites_keeps_crlf_out_of_the_inserted_line():
    # The insert is planned as LF text, so a restored CRLF file must not end up with a
    # bare LF on the line reconcile added.
    path = Path("downstream.md")
    source = b"---\r\nid: d\r\nderives_from:\r\n  - ref: a#x\r\n---\r\nbody\r\n"
    expected_after = (
        b"---\r\nid: d\r\nderives_from:\r\n  - ref: a#x\r\n    seen: newhash\r\n---\r\nbody\r\n"
    )

    rewrites = plan_rewrites({path: {"a#x": "newhash"}}, lambda _path: source)

    assert len(rewrites) == 1
    assert rewrites[0].after == expected_after


def test_plan_rewrites_wraps_reader_error_with_path():
    path = Path("downstream.md")

    def raise_os_error(_path: Path) -> bytes:
        raise OSError("disk vanished")

    with pytest.raises(UnreadableDocError) as exc_info:
        plan_rewrites({path: {"a#x": "newhash"}}, raise_os_error)
    assert str(exc_info.value) == "cannot read downstream.md to reconcile: disk vanished"


def test_plan_rewrites_names_unclosed_frontmatter_source():
    path = Path("downstream.md")
    source = b"---\nid: d\nderives_from:\n  - ref: a#x\n"

    with pytest.raises(UnreadableDocError) as exc_info:
        plan_rewrites({path: {"a#x": "newhash"}}, lambda _path: source)

    assert str(exc_info.value) == (
        "unclosed YAML frontmatter in downstream.md: add a closing '---' fence"
    )


def test_plan_rewrites_wraps_invalid_utf8_with_path():
    path = Path("downstream.md")

    with pytest.raises(UnreadableDocError) as exc_info:
        plan_rewrites({path: {"a#x": "newhash"}}, lambda _path: b"\xff")

    assert str(exc_info.value).startswith("cannot read downstream.md to reconcile: ")


def test_plan_rewrites_skips_file_when_updates_already_applied():
    path = Path("downstream.md")
    source = b"---\nid: d\nderives_from:\n  - ref: a#x\n    seen: same\n---\nbody\n"

    assert plan_rewrites({path: {"a#x": "same"}}, lambda _path: source) == []


def test_plan_rewrites_preserves_plan_order():
    first = Path("first.md")
    second = Path("second.md")
    text_by_path = {
        first: b"---\nid: one\nderives_from:\n  - ref: up#first\n---\nbody\n",
        second: b"---\nid: two\nderives_from:\n  - ref: up#second\n---\nbody\n",
    }

    rewrites = plan_rewrites(
        {first: {"up#first": "hashone"}, second: {"up#second": "hashtwo"}},
        text_by_path.__getitem__,
    )

    assert [rewrite.path for rewrite in rewrites] == [first, second]


def test_apply_reconcile_sets_seen_and_preserves_body():
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\n# Body\nkeep me\n"
    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))
    assert "seen: newhash" in out
    assert "old" not in out
    assert out.endswith("# Body\nkeep me\n")
    assert applied == {"a#x"}


def test_apply_reconcile_adds_missing_seen():
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n---\nbody\n"
    out, applied = apply_reconcile(text, {"a#x": "h"}, Path("downstream.md"))
    assert "seen: h" in out
    assert applied == {"a#x"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nbody\n",
            "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: new\n---\nbody\n",
        ),
        (
            "---\nid: d\nderives_from:\n- ref: a#x\n  seen: old\n---\nbody\n",
            "---\nid: d\nderives_from:\n- ref: a#x\n  seen: new\n---\nbody\n",
        ),
    ],
)
def test_apply_reconcile_preserves_derives_from_indentation(text: str, expected: str):
    out, applied = apply_reconcile(text, {"a#x": "new"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


@pytest.mark.parametrize(
    ("source_entry", "expected_entry"),
    [
        ("  - ref: a#x\n", "  - ref: a#x\n    seen: new\n"),
        ("- ref: a#x\n", "- ref: a#x\n  seen: new\n"),
        ("  - {ref: a#x}\n", "  - {ref: a#x, seen: new}\n"),
    ],
)
def test_apply_reconcile_adds_seen_without_reformatting_entry(
    source_entry: str, expected_entry: str
):
    text = f"---\nid: d\nderives_from:\n{source_entry}---\nbody\n"
    expected = f"---\nid: d\nderives_from:\n{expected_entry}---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "new"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_replaces_null_seen_without_moving_comment():
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: # keep\n---\nbody\n"
    expected = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: new # keep\n---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "new"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_replaces_a_null_seen_whose_key_comment_holds_a_colon():
    # An explicit key can carry a comment between the key and its value indicator, so the
    # first colon after the key is not necessarily the one that opens the value.
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    ? seen # why: blank\n    :\n---\nbody\n"
    expected = (
        "---\nid: d\nderives_from:\n  - ref: a#x\n    ? seen # why: blank\n    : new\n---\nbody\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "new"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _validated_reconcile_meta(out).derives_from[0].seen == "new"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "---\nid: d\nderives_from:\n  - ref: a#x\n    ? seen\n---\nbody\n",
            "---\nid: d\nderives_from:\n  - ref: a#x\n    ? seen\n    : new\n---\nbody\n",
            id="trailing-key",
        ),
        pytest.param(
            "---\nid: d\nderives_from:\n  - ? seen\n    ref: a#x\n---\nbody\n",
            "---\nid: d\nderives_from:\n  - ? seen\n    : new\n    ref: a#x\n---\nbody\n",
            id="leading-key",
        ),
        pytest.param(
            "---\nid: d\nderives_from:\n  - ref: a#x\n    ? seen # note\n---\nbody\n",
            "---\nid: d\nderives_from:\n  - ref: a#x\n    ? seen # note\n    : new\n---\nbody\n",
            id="commented-key",
        ),
        pytest.param(
            "---\nid: d\nderives_from:\n  - {ref: a#x, ? seen}\n---\nbody\n",
            "---\nid: d\nderives_from:\n  - {ref: a#x, ? seen: new}\n---\nbody\n",
            id="flow-entry",
        ),
    ],
)
def test_apply_reconcile_writes_the_value_indicator_an_explicit_seen_key_lacks(
    text: str, expected: str
):
    # `? seen` alone is a key whose value is null, so there is no `:` to write after and the
    # next pair's indicator is the first one in the entry.
    out, applied = apply_reconcile(text, {"a#x": "new"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _validated_reconcile_meta(out).derives_from[0].seen == "new"


def test_apply_reconcile_replaces_inline_flow_null_without_corrupting_frontmatter():
    text = "---\nid: d\nderives_from:\n- {ref: a#x, seen: }\n---\nbody\n"
    expected = "---\nid: d\nderives_from:\n- {ref: a#x, seen: newhash}\n---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _validated_reconcile_meta(out).derives_from[0].seen == "newhash"


def test_apply_reconcile_replaces_block_scalar_without_consuming_final_newline():
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "- ref: a#x\n"
        "  seen: |-\n"
        "    oldhash\n"
        "tickets:\n"
        "- T-1\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\nid: d\nderives_from:\n- ref: a#x\n  seen: newhash\ntickets:\n- T-1\n---\nbody\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    meta = _validated_reconcile_meta(out)
    assert meta.derives_from[0].seen == "newhash"
    assert meta.tickets == ["T-1"]


@pytest.mark.parametrize(
    ("header", "expected_value"),
    [
        ("|  # keep this", "newhash  # keep this"),
        (">- # keep this", "newhash # keep this"),
        ("|2 # indented", "newhash # indented"),
        ("!!str | # tagged", "newhash # tagged"),
    ],
)
def test_apply_reconcile_keeps_the_comment_on_a_replaced_block_scalar_header(
    header: str, expected_value: str
):
    # A block scalar's token spans its header, the comment on it, and the contents, so
    # replacing the whole span drops a comment that a plain scalar on the same line keeps.
    # The comment is the author's, not the value's, and reload verification cannot see it go.
    text = f"---\nid: d\nderives_from:\n- ref: a#x\n  seen: {header}\n    oldhash\n---\nbody\n"
    expected = f"---\nid: d\nderives_from:\n- ref: a#x\n  seen: {expected_value}\n---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _validated_reconcile_meta(out).derives_from[0].seen == "newhash"


@pytest.mark.parametrize(
    ("source_value", "expected_value"),
    [
        ("!!str # keep\n    old", "# keep\n    newhash"),
        ("&h # keep\n    old", "# keep\n    newhash"),
        ("!!str  # keep\n  # and this\n    old", "# keep\n  # and this\n    newhash"),
        ("&h # first\n    !!str # second\n    old", "# first\n    # second\n    newhash"),
        ("!!str &h # keep\n    old", "# keep\n    newhash"),
    ],
)
def test_apply_reconcile_keeps_a_comment_written_between_a_seens_properties_and_its_value(
    source_value: str, expected_value: str
):
    # An anchor or a tag is dropped when the value it carries is replaced, and the span
    # reaching back to it swallowed any comment written between the two. A reload cannot see
    # that loss, so each property is now removed on its own, up to a comment rather than past
    # one, which leaves the comment where its author put it.
    text = f"---\nid: d\nderives_from:\n- ref: a#x\n  seen: {source_value}\ntitle: *h\n---\nbody\n"
    expected = (
        f'---\nid: d\nderives_from:\n- ref: a#x\n  seen: {expected_value}\ntitle: &h "old"\n'
        "---\nbody\n"
    )
    if "&h" not in source_value:
        text = text.replace("title: *h\n", "")
        expected = expected.replace('title: &h "old"\n', "")

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _validated_reconcile_meta(out).derives_from[0].seen == "newhash"


@pytest.mark.parametrize(
    ("source_root", "expected_root"),
    [
        (
            "!!omap\n- id: d\n- derives_from:\n    - ref: a#x\n      seen: old\n",
            "!!omap\n- id: d\n- derives_from:\n    - ref: a#x\n      seen: newhash\n",
        ),
        (
            "!!omap\n- id: d\n- derives_from:\n  - ref: a#x\n",
            "!!omap\n- id: d\n- derives_from:\n  - ref: a#x\n    seen: newhash\n",
        ),
        (
            "!!omap [{id: d}, {derives_from: [{ref: a#x, seen: old}]}]\n",
            "!!omap [{id: d}, {derives_from: [{ref: a#x, seen: newhash}]}]\n",
        ),
        (
            "!!omap\n- id: d  # keep\n- derives_from: [{ref: a#x}]  # and this\n",
            "!!omap\n- id: d  # keep\n- derives_from: [{ref: a#x, seen: newhash}]  # and this\n",
        ),
    ],
)
def test_apply_reconcile_updates_an_ordered_map_at_the_frontmatter_root(
    source_root: str, expected_root: str
):
    # A root written as an ordered map loads as a mapping and validates as a lattice node,
    # so its `derives_from` has to be found among the one-pair mappings its source spells
    # rather than looked up in a mapping that is not there.
    text = f"---\n{source_root}---\nbody\n"
    expected = f"---\n{expected_root}---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _validated_reconcile_meta(out).derives_from[0].seen == "newhash"


def test_apply_reconcile_leaves_an_alias_of_an_updated_entry_untouched():
    # The alias reads its value through the anchor, which this run already updated, so
    # rewriting the alias site would only churn a line the author wrote deliberately.
    text = "---\nid: d\nderives_from:\n  - &edge {ref: a#x}\n  - *edge\n---\nbody\n"
    expected = (
        "---\nid: d\nderives_from:\n  - &edge {ref: a#x, seen: newhash}\n  - *edge\n---\nbody\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        "newhash",
    ]


def test_apply_reconcile_replaces_targeted_scalar_alias_not_its_anchor():
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: b#y\n"
        "    seen: &shared oldhash\n"
        "  - ref: a#x\n"
        "    seen: *shared\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: b#y\n"
        "    seen: &shared oldhash\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "oldhash",
        "newhash",
    ]


def test_apply_reconcile_relocates_targeted_seen_anchor_to_untargeted_alias():
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: &shared oldhash\n"
        "  - ref: b#y\n"
        "    seen: *shared\n"
        "  - ref: c#z\n"
        "    seen: *shared\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        "  - ref: b#y\n"
        '    seen: &shared "oldhash"\n'
        "  - ref: c#z\n"
        "    seen: *shared\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        "oldhash",
        "oldhash",
    ]


def test_apply_reconcile_drops_seen_anchor_when_all_alias_consumers_are_targeted():
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: &shared oldhash\n"
        "  - ref: b#y\n"
        "    seen: *shared\n"
        "  - ref: c#z\n"
        "    seen: *shared\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: new-a\n"
        "  - ref: b#y\n"
        "    seen: new-b\n"
        "  - ref: c#z\n"
        "    seen: new-c\n"
        "---\n"
        "body\n"
    )
    updates = {"a#x": "new-a", "b#y": "new-b", "c#z": "new-c"}

    out, applied = apply_reconcile(text, updates, Path("downstream.md"))

    assert out == expected
    assert applied == set(updates)
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "new-a",
        "new-b",
        "new-c",
    ]


def test_apply_reconcile_relocates_seen_anchor_to_untouched_ticket_alias():
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: &shared oldhash\n"
        "tickets: [*shared]\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        'tickets: [&shared "oldhash"]\n'
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    meta = _validated_reconcile_meta(out)
    assert meta.derives_from[0].seen == "newhash"
    assert meta.tickets == ["oldhash"]


def test_apply_reconcile_relocates_tagged_seen_anchor_as_a_string():
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: &shared !!str 123\n"
        "  - ref: b#y\n"
        "    seen: *shared\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        "  - ref: b#y\n"
        '    seen: &shared "123"\n'
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        "123",
    ]


@pytest.mark.parametrize("source_seen", ["&shared null", "&shared ~", "&shared"])
def test_apply_reconcile_relocates_anchored_null_with_null_semantics(source_seen: str):
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        f"    seen: {source_seen}\n"
        "  - ref: b#y\n"
        "    seen: *shared\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        "  - ref: b#y\n"
        "    seen: &shared null\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        None,
    ]


def test_reconcile_reads_through_the_pure_python_parser():
    # Installing the optional `ruamel.yaml.clib` accelerator, which any other package may pull
    # in, otherwise switches a safe loader to the C parser. That parser reports coarser source
    # marks and exposes no scanner, so every offset this module measures would move: the
    # implementation is part of the compatibility surface AD-26 records, not a detail.
    assert reconcile_module._yaml().Parser is Parser


def test_apply_reconcile_relocates_block_seen_anchor_into_flow_collection_safely():
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: &shared |\n"
        "      old value\n"
        "tickets: [*shared]\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        'tickets: [&shared "old value\\n"]\n'
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    meta = _validated_reconcile_meta(out)
    assert meta.derives_from[0].seen == "newhash"
    assert meta.tickets == ["old value\n"]


@pytest.mark.parametrize(
    "char",
    ["\x7f", "\x85", "\x9f", "\u2028", "\u2029", "\ufeff", "\t"],
)
def test_apply_reconcile_relocates_a_seen_anchor_holding_an_unprintable_character(char: str):
    # A character YAML admits only as an escape has to be written back as one: emitting it
    # raw would either leave the document unparseable or fold the value across lines, and
    # either way the reparse gate would refuse a rewrite the document itself allows.
    escape = "\\t" if char == "\t" else f"\\u{ord(char):04x}"
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        f'    seen: &shared "old{escape}"\n'
        "tickets: [*shared]\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        f'tickets: [&shared "old{escape}"]\n'
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    meta = _validated_reconcile_meta(out)
    assert meta.derives_from[0].seen == "newhash"
    assert meta.tickets == [f"old{char}"]


def test_apply_reconcile_relocates_a_multiline_tagged_seen_anchor_in_its_own_type():
    # Only an explicit tag makes a multiline scalar anything but a string, so the relocated
    # value keeps that tag and takes its scalar quoted rather than being rendered through
    # `str`, which would retype it and lose the whole rewrite to the reparse gate.
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: &shared !!int |\n"
        "      42\n"
        "tickets: [*shared]\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        'tickets: [&shared !!int "42\\n"]\n'
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _reloaded_tickets(out) == [42]


def test_apply_reconcile_adds_seen_to_flow_mapping_with_commented_trailing_comma():
    text = "---\nid: d\nderives_from:\n- {ref: a#x, # keep\n  }\n---\nbody\n"
    expected = "---\nid: d\nderives_from:\n- {ref: a#x, # keep\n  seen: new}\n---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "new"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_adds_seen_after_comment_before_flow_mapping_trailing_comma():
    text = "---\nid: d\nderives_from:\n- {ref: a#x # keep\n   ,}\n---\nbody\n"
    expected = "---\nid: d\nderives_from:\n- {ref: a#x # keep\n   , seen: new}\n---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "new"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_adds_seen_before_the_next_indented_list_item():
    # An indented block entry ends at the *next* item's dash, so appending at the parsed
    # mapping end would splice the new key into the following entry.
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n  - ref: b#y\n    seen: q\n---\nbody\n"
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        "  - ref: b#y\n"
        "    seen: q\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        "q",
    ]


def test_apply_reconcile_adds_seen_above_a_trailing_entry_comment():
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    # keep\n"
        "  - ref: b#y\n"
        "    seen: q\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        "    # keep\n"
        "  - ref: b#y\n"
        "    seen: q\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_adds_seen_after_an_entrys_trailing_block_scalar():
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    note: |\n"
        "      text\n"
        "  - ref: b#y\n"
        "    seen: q\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    note: |\n"
        "      text\n"
        "    seen: newhash\n"
        "  - ref: b#y\n"
        "    seen: q\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_quotes_an_all_digit_hash_so_it_reloads_as_a_string():
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nbody\n"
    digits = "1" * 64
    expected = f'---\nid: d\nderives_from:\n  - ref: a#x\n    seen: "{digits}"\n---\nbody\n'

    out, applied = apply_reconcile(text, {"a#x": digits}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _validated_reconcile_meta(out).derives_from[0].seen == digits


@pytest.mark.parametrize("source_seen", ["true", "123", "1.10"])
def test_apply_reconcile_relocates_a_non_string_seen_anchor_without_retyping_it(
    source_seen: str,
):
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        f"    seen: &shared {source_seen}\n"
        "tickets: [*shared]\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        f"tickets: [&shared {source_seen}]\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_replaces_a_seen_scalar_whose_tag_precedes_its_anchor():
    # A node written tag first starts at its anchor, so overwriting from the node's own mark
    # would leave the tag behind to retype the hash written under it.
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: !!float &shared 12\n---\nbody\n"
    expected = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: newhash\n---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _validated_reconcile_meta(out).derives_from[0].seen == "newhash"


def test_apply_reconcile_relocates_a_tagged_seen_anchor_without_retyping_it():
    # The scalar token excludes the tag, so relocating the token alone would republish an
    # explicit float as an implicit integer. Whole-document verification cannot catch that
    # on its own, since 12 == 12.0.
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: &shared !!float 12\n"
        "tickets: [*shared]\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        "tickets: [&shared !!float 12]\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    tickets = _reloaded_tickets(out)
    assert tickets == [12.0]
    assert [type(ticket) for ticket in tickets] == [float]


@pytest.mark.parametrize(
    ("properties", "relocated"),
    [
        ("&shared !!float\n      12", "&shared !!float 12"),
        ("&shared\n      12", "&shared 12"),
        ("!!float &shared 12", "&shared !!float 12"),
        (
            "&shared !<tag:yaml.org,2002:float> 12",
            "&shared !<tag:yaml.org,2002:float> 12",
        ),
    ],
)
def test_apply_reconcile_relocates_a_tagged_seen_anchor_written_across_lines(
    properties: str, relocated: str
):
    # A node's properties can sit on their own line and in either order, so each part of the
    # relocated value comes from its own token rather than from one slice of the node.
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        f"    seen: {properties}\n"
        "tickets: [*shared]\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        f"tickets: [{relocated}]\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _reloaded_tickets(out) == [12]


@pytest.mark.filterwarnings("ignore:(?s).*duplicate anchor.*")
def test_apply_reconcile_leaves_an_alias_bound_to_a_later_anchor_definition_alone():
    # `*shared` reads the second definition, so relocating the first one's value onto it
    # would silently rewrite an untargeted edge's recorded hash.
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: &shared old1\n"
        "  - ref: b#y\n"
        "    seen: &shared old2\n"
        "  - ref: c#z\n"
        "    seen: *shared\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        "  - ref: b#y\n"
        "    seen: &shared old2\n"
        "  - ref: c#z\n"
        "    seen: *shared\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_updates_an_aliased_derives_from_list():
    text = (
        "---\nbase: &edges\n  - ref: a#x\n    seen: old\nid: d\nderives_from: *edges\n---\nbody\n"
    )
    expected = (
        "---\nbase: &edges\n  - ref: a#x\n    seen: newhash\nid: d\nderives_from: *edges\n"
        "---\nbody\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_updates_a_merge_key_provided_derives_from_list():
    text = (
        "---\n"
        "base: &shared\n"
        "  derives_from:\n"
        "    - ref: a#x\n"
        "      seen: old\n"
        "id: d\n"
        "<<: *shared\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "base: &shared\n"
        "  derives_from:\n"
        "    - ref: a#x\n"
        "      seen: newhash\n"
        "id: d\n"
        "<<: *shared\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_updates_a_tagged_merge_key_provided_derives_from_list():
    # The loader merges on the resolved tag, which an explicit `!!merge` carries just as the
    # plain `<<` spelling does, so matching only the shorthand loses the inherited source.
    text = (
        "---\n"
        "base: &shared\n"
        "  derives_from:\n"
        "    - ref: a#x\n"
        "      seen: old\n"
        "id: d\n"
        "!!merge inherited: *shared\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "base: &shared\n"
        "  derives_from:\n"
        "    - ref: a#x\n"
        "      seen: newhash\n"
        "id: d\n"
        "!!merge inherited: *shared\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_updates_a_merge_provided_list_beside_an_aliased_key():
    # A key written as an alias carries no tag of its own, so the scan for the merge key that
    # provides derives_from has to pass over it rather than read one off it.
    text = (
        "---\n"
        "base: &shared\n"
        "  derives_from:\n"
        "    - ref: a#x\n"
        "      seen: old\n"
        "id: &name d\n"
        "? *name\n"
        ": kept\n"
        "<<: *shared\n"
        "---\n"
        "body\n"
    )
    expected = text.replace("      seen: old\n", "      seen: newhash\n")

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


@pytest.mark.parametrize("seen_key", ["? *name\n    : old", "*name : old"])
def test_apply_reconcile_updates_a_seen_key_spelled_through_an_alias(seen_key: str):
    # The loader resolves an alias key to the string it names, so an entry can hold a `seen`
    # member whose key is never spelled `seen` in source. Appending a second one would make
    # the document unreconcilable rather than update the value the loader reads.
    text = (
        f"---\nid: d\ntitle: &name seen\nderives_from:\n  - ref: a#x\n    {seen_key}\n---\nbody\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "title: &name seen\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        f"    {seen_key.replace('old', 'newhash')}\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _validated_reconcile_meta(out).derives_from[0].seen == "newhash"


@pytest.mark.parametrize(
    ("source_entry", "expected_entry"),
    [
        (
            "  - !!omap\n    - ref: a#x\n    - seen: old\n",
            "  - !!omap\n    - ref: a#x\n    - seen: newhash\n",
        ),
        (
            "  - !!omap\n    - ref: a#x\n",
            "  - !!omap\n    - ref: a#x\n    - seen: newhash\n",
        ),
        (
            "- !!omap\n  - ref: a#x\n",
            "- !!omap\n  - ref: a#x\n  - seen: newhash\n",
        ),
        (
            "  - !!omap\n    - ref: a#x   # keep\n    - seen: old  # and this\n",
            "  - !!omap\n    - ref: a#x   # keep\n    - seen: newhash  # and this\n",
        ),
        (
            "  - !!omap [{ref: a#x}, {seen: old}]\n",
            "  - !!omap [{ref: a#x}, {seen: newhash}]\n",
        ),
        (
            "  - !!omap [{ref: a#x}]\n",
            "  - !!omap [{ref: a#x}, {seen: newhash}]\n",
        ),
    ],
)
def test_apply_reconcile_updates_an_ordered_map_entry(source_entry: str, expected_entry: str):
    # An `!!omap` entry loads as a mapping and validates as an edge, so it stays reconcilable
    # even though its source is a sequence: the pair an edit targets sits in a one-pair
    # mapping inside it, and a missing one is appended as an item rather than as a key.
    text = f"---\nid: d\nderives_from:\n{source_entry}---\nbody\n"
    expected = f"---\nid: d\nderives_from:\n{expected_entry}---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _validated_reconcile_meta(out).derives_from[0].seen == "newhash"


def test_apply_reconcile_leaves_an_alias_of_an_updated_ordered_map_entry_untouched():
    # An entry written as an ordered map is a sequence rather than a mapping, so the guard
    # that recognizes an entry this run already updated has to accept one too. Rewriting the
    # alias site instead would discard the author's spelling and stop that entry being an
    # ordered map, neither of which the update asked for.
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - &edge !!omap\n"
        "    - ref: a#x\n"
        "    - seen: old\n"
        "  - *edge\n"
        "---\n"
        "body\n"
    )
    expected = text.replace("    - seen: old\n", "    - seen: newhash\n")

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        "newhash",
    ]


def test_apply_reconcile_writes_an_ordered_map_pair_spelled_through_an_alias_at_its_item():
    # The mapping this item names is shared with the key that defines it, so editing the
    # definition would rewrite a member the edge does not own. An ordered map item holds
    # exactly one pair, and this one holds only `seen`, so writing that pair out at the item
    # keeps the entry's value exact and leaves the definition alone.
    text = (
        "---\n"
        "id: d\n"
        "shared: &pair\n"
        "  seen: old\n"
        "derives_from:\n"
        "  - !!omap\n"
        "    - ref: a#x\n"
        "    - *pair\n"
        "---\n"
        "body\n"
    )
    expected = text.replace("    - *pair\n", "    - {seen: newhash}\n")

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    raw_meta, _ = split_frontmatter(out, Path("downstream.md"))
    reloaded = YAML(typ="safe").load(raw_meta)
    assert reloaded["shared"] == {"seen": "old"}
    assert reloaded["derives_from"][0]["seen"] == "newhash"


def test_apply_reconcile_updates_an_entry_inheriting_seen_through_a_merge_key():
    # The loader flattens a merge into a copy rather than giving both entries one object, so
    # the inheriting entry is not updated by assigning to the one that spells `seen`. The
    # rewrite changes what both read all the same, and an expectation modelling only the
    # entry that was edited would refuse a rewrite that is exactly right.
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - &base\n"
        "    ref: a#x\n"
        "    seen: old\n"
        "  - <<: *base\n"
        "    ref: b#y\n"
        "---\n"
        "body\n"
    )
    expected = text.replace("    seen: old\n", "    seen: newhash\n")

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        "newhash",
    ]


def test_apply_reconcile_updates_an_entry_inheriting_a_seen_that_is_appended():
    # The merge source carries no `seen` yet, so one is written into it and the inheriting
    # entry starts reading it. Nothing is written at that entry, and the expectation has to
    # account for it anyway.
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - &base\n"
        "    ref: a#x\n"
        "  - <<: *base\n"
        "    ref: b#y\n"
        "---\n"
        "body\n"
    )
    expected = text.replace("    ref: a#x\n", "    ref: a#x\n    seen: newhash\n")

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        "newhash",
    ]


@pytest.mark.parametrize(
    ("entry", "edited"),
    [
        pytest.param(
            "  - &edge !!omap\n    - ref: a#x\n    - seen: old\n",
            "  - &edge !!omap\n    - ref: a#x\n    - seen: newhash\n",
            id="spelled",
        ),
        pytest.param(
            "  - &edge !!omap\n    - ref: a#x\n",
            "  - &edge !!omap\n    - ref: a#x\n    - seen: newhash\n",
            id="appended",
        ),
    ],
)
def test_apply_reconcile_updates_an_entry_inheriting_seen_from_an_ordered_map(
    entry: str, edited: str
):
    # The loader merges an ordered map as the one-pair mappings it is written as, so the
    # entry inheriting from one reads whichever `seen` this rewrite writes into it, whether
    # that hash replaces one already there or is appended as a new item.
    text = f"---\nid: d\nderives_from:\n{entry}  - <<: *edge\n    ref: b#y\n---\nbody\n"
    expected = f"---\nid: d\nderives_from:\n{edited}  - <<: *edge\n    ref: b#y\n---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        "newhash",
    ]


def test_apply_reconcile_updates_an_entry_merging_the_ordered_map_item_that_holds_seen():
    # This merge names one item of the ordered map rather than the map itself, and it is the
    # item the hash is written into, so the entry reads the new one.
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - !!omap\n"
        "    - ref: a#x\n"
        "    - &seen_item {seen: old}\n"
        "  - <<: *seen_item\n"
        "    ref: b#y\n"
        "---\n"
        "body\n"
    )
    expected = text.replace("{seen: old}", "{seen: newhash}")

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        "newhash",
    ]


def test_apply_reconcile_leaves_an_entry_merging_an_ordered_map_item_without_seen_alone():
    # This merge names the one item of the ordered map the rewrite does not touch, so the
    # entry inherits no `seen` before or after it. Treating every item of an updated ordered
    # map as changed would expect a hash here that the reload correctly never shows.
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - !!omap\n"
        "    - &ref_item {ref: a#x}\n"
        "    - seen: old\n"
        "  - <<: *ref_item\n"
        "    ref: b#y\n"
        "---\n"
        "body\n"
    )
    expected = text.replace("    - seen: old\n", "    - seen: newhash\n")

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        None,
    ]


def test_apply_reconcile_leaves_a_merge_reading_the_definition_an_ordered_map_item_aliases():
    # The targeted entry spells its `seen` through an alias, so the rewrite writes a pair of
    # its own at that item and leaves the definition alone. The entry merging that definition
    # keeps reading the old hash, and so does the entry the definition is written in.
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - !!omap\n"
        "    - ref: c#w\n"
        "    - &shared_pair {seen: shared}\n"
        "  - !!omap\n"
        "    - ref: a#x\n"
        "    - *shared_pair\n"
        "  - <<: *shared_pair\n"
        "    ref: b#y\n"
        "---\n"
        "body\n"
    )
    expected = text.replace("    - *shared_pair\n", "    - {seen: newhash}\n")

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "shared",
        "newhash",
        "shared",
    ]


def test_apply_reconcile_updates_entries_inheriting_an_ordered_map_seen_through_a_chain():
    # The entry that merges the ordered map is itself merged by a third, so the hash written
    # into the ordered map reaches both and the expectation has to carry it that far.
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - &edge !!omap\n"
        "    - ref: a#x\n"
        "    - seen: old\n"
        "  - &mid\n"
        "    <<: *edge\n"
        "    ref: b#y\n"
        "  - <<: *mid\n"
        "    ref: c#z\n"
        "---\n"
        "body\n"
    )
    expected = text.replace("    - seen: old\n", "    - seen: newhash\n")

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        "newhash",
        "newhash",
    ]


def test_apply_reconcile_leaves_an_entry_spelling_its_own_seen_beside_a_merge_alone():
    # This entry names the same anchored scalar the updated entry does, but through a pair
    # of its own rather than through a merge, so the anchor it reads is untouched and it
    # keeps the old hash. Treating it as inheriting would expect a change that never lands.
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: &shared old\n"
        "  - ref: b#y\n"
        "    seen: *shared\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newhash\n"
        "  - ref: b#y\n"
        '    seen: &shared "old"\n'
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == [
        "newhash",
        "old",
    ]


def test_apply_reconcile_relocates_a_seen_anchor_out_of_an_ordered_map_entry():
    text = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - !!omap\n"
        "    - ref: a#x\n"
        "    - seen: &shared oldhash\n"
        "tickets: [*shared]\n"
        "---\n"
        "body\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - !!omap\n"
        "    - ref: a#x\n"
        "    - seen: newhash\n"
        'tickets: [&shared "oldhash"]\n'
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert _validated_reconcile_meta(out).tickets == ["oldhash"]


@pytest.mark.parametrize(
    ("prefix", "open_fence", "close_fence", "tail"),
    [
        ("﻿", "---", "---", "\nbody\n"),
        ("", "---   ", "---  ", "\nbody\n"),
        ("﻿", "---", "---", ""),
        ("", "---", "---", "\n"),
    ],
)
def test_apply_reconcile_reattaches_the_document_it_was_given(
    prefix: str, open_fence: str, close_fence: str, tail: str
):
    # A byte-order mark, the spelling of either fence, and whether the closing one ends the
    # file are the author's, so rewriting the frontmatter must not restyle any of them.
    meta = "id: d\nderives_from:\n  - ref: a#x\n    seen: {seen}\n"
    text = f"{prefix}{open_fence}\n{meta.format(seen='old')}{close_fence}{tail}"
    expected = f"{prefix}{open_fence}\n{meta.format(seen='newhash')}{close_fence}{tail}"

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_refuses_source_edits_that_do_not_reload_as_planned(
    monkeypatch: pytest.MonkeyPatch,
):
    # The commit transaction never re-reads what it stages, so a mis-measured span has to
    # be refused here rather than published durably.
    monkeypatch.setattr(
        reconcile_module,
        "_apply_source_edits",
        lambda *_: "id: d\nderives_from: []\n",
    )
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nbody\n"

    with pytest.raises(UnreadableDocError, match="would not reproduce the derives_from entries"):
        apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))


def test_apply_reconcile_refuses_source_edits_that_change_other_frontmatter_keys(
    monkeypatch: pytest.MonkeyPatch,
):
    # Relocating a seen anchor edits bytes outside derives_from, so checking the edges
    # alone would let a mis-measured relocation span through to a durable write.
    monkeypatch.setattr(
        reconcile_module,
        "_apply_source_edits",
        lambda *_: "id: d\nderives_from:\n  - ref: a#x\n    seen: newhash\ntickets: [T-2]\n",
    )
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\ntickets: [T-1]\n---\nbody\n"

    with pytest.raises(UnreadableDocError, match="frontmatter outside derives_from"):
        apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))


@pytest.mark.parametrize("source_seen", ["!!int 12", "!!null ~", "!!str 12"])
def test_apply_reconcile_replaces_a_tagged_seen_scalar_with_its_tag(source_seen: str):
    # The tag belongs to the value being replaced: leaving it in place would retype the new
    # hash, or reject it outright, on the next read.
    text = f"---\nid: d\nderives_from:\n  - ref: a#x\n    seen: {source_seen}\n---\nbody\n"
    expected = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: newhash\n---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == ["newhash"]


def test_apply_reconcile_refuses_a_collection_seen_as_a_project_error():
    # A list has no single scalar to splice, so this shape is refused rather than guessed at.
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: [1, 2]\n---\nbody\n"

    with pytest.raises(UnreadableDocError, match="entry seen is malformed"):
        apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))


def test_apply_reconcile_refuses_self_referential_frontmatter_as_a_project_error():
    # A cyclic document compares without bound, so the rewrite cannot be verified; the CLI
    # gets a clean refusal instead of a RecursionError traceback.
    text = "---\nid: d\nr: &r\n  self: *r\nderives_from:\n  - ref: a#x\n    seen: old\n---\nbody\n"

    with pytest.raises(UnreadableDocError, match="self-referential"):
        apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))


def test_apply_reconcile_quotes_a_replacement_the_constructor_rejects():
    # The plain-scalar probe loads the replacement, and a safe constructor raises a bare
    # ValueError for an impossible timestamp; quoting it keeps it a string either way.
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "2026-13-45"}, Path("downstream.md"))

    assert out == '---\nid: d\nderives_from:\n  - ref: a#x\n    seen: "2026-13-45"\n---\nbody\n'
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == ["2026-13-45"]


def test_apply_reconcile_quotes_a_hash_this_documents_yaml_version_would_retype():
    # Under 1.1 a bare `y` reloads as a boolean, so the plain-scalar probe has to run under
    # the document's own declared version rather than the loader default.
    text = (
        "---\n%YAML 1.1\n--- !!map\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nbody\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "y"}, Path("downstream.md"))

    assert out == (
        '---\n%YAML 1.1\n--- !!map\nid: d\nderives_from:\n  - ref: a#x\n    seen: "y"\n---\nbody\n'
    )
    assert applied == {"a#x"}


def test_apply_reconcile_adds_seen_after_an_entry_key_with_an_empty_value():
    # An implicit null carries the *next* token's mark, so anchoring the append to it would
    # splice the new key into the following entry.
    text = (
        "---\nid: d\nderives_from:\n  - ref: a#x\n    note:\n  - ref: b#y\n    seen: q\n---\nbody\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    note:\n"
        "    seen: newhash\n"
        "  - ref: b#y\n"
        "    seen: q\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


@pytest.mark.parametrize(
    ("tail", "appended_after"),
    [
        pytest.param("    ? note\n    :\n", "    ? note\n    :\n", id="explicit-indicator"),
        pytest.param("    ? note # c\n    :\n", "    ? note # c\n    :\n", id="commented-key"),
        pytest.param("    ? note\n", "    ? note\n", id="no-indicator"),
    ],
)
def test_apply_reconcile_appends_seen_after_a_trailing_explicit_null(
    tail: str, appended_after: str
):
    # An explicit null spells its value as a `:` on its own line, so the key's end is not
    # where that pair's source stops.
    text = f"---\nid: d\nderives_from:\n  - ref: a#x\n{tail}---\nbody\n"
    expected = (
        f"---\nid: d\nderives_from:\n  - ref: a#x\n{appended_after}    seen: newhash\n---\nbody\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_inserts_seen_into_an_explicit_key_entry():
    # An explicit-key entry starts at its `?` indicator, one indent short of the key, so
    # the key indentation is the mapping's own column rather than the first key's.
    text = "---\nid: d\nderives_from:\n  - ? ref\n    : a#x\n---\nbody\n"
    expected = "---\nid: d\nderives_from:\n  - ? ref\n    : a#x\n    seen: newhash\n---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == ["newhash"]


@pytest.mark.parametrize("entry_property", ["&edge", "!!map"])
def test_apply_reconcile_indents_seen_under_an_entry_line_property(entry_property: str):
    # An anchor or a tag on the sequence line starts the mapping node above and left of its
    # first key, so the node's own start mark is not the column a new key belongs at.
    text = f"---\nid: d\nderives_from:\n  - {entry_property}\n      ref: a#x\n---\nbody\n"
    expected = (
        f"---\nid: d\nderives_from:\n  - {entry_property}\n"
        "      ref: a#x\n      seen: newhash\n---\nbody\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == ["newhash"]


def test_apply_reconcile_inserts_seen_after_a_multi_line_flow_value():
    # The entry's last leaf sits inside a flow sequence that closes on its own line, so
    # anchoring the insert to that leaf would splice the new key into an open collection.
    text = (
        "---\nid: d\nderives_from:\n  - ref: a#x\n    tags: [\n      a,\n      b\n    ]\n"
        "---\nbody\n"
    )
    expected = (
        "---\n"
        "id: d\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    tags: [\n"
        "      a,\n"
        "      b\n"
        "    ]\n"
        "    seen: newhash\n"
        "---\n"
        "body\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_does_not_leak_a_yaml_directive_into_the_next_document():
    # YAML.version is sticky, and under 1.1 semantics an octal-looking hash would be
    # spliced in bare and then reload as an integer on the next check.
    directive = (
        "---\n%YAML 1.1\n--- !!map\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nbody\n"
    )
    apply_reconcile(directive, {"a#x": "newhash"}, Path("directive.md"))

    text = "---\nid: e\nderives_from:\n  - ref: a#x\n    seen: old\n---\nbody\n"
    out, applied = apply_reconcile(text, {"a#x": "0o17"}, Path("downstream.md"))

    assert out == '---\nid: e\nderives_from:\n  - ref: a#x\n    seen: "0o17"\n---\nbody\n'
    assert applied == {"a#x"}
    assert [edge.seen for edge in _validated_reconcile_meta(out).derives_from] == ["0o17"]


def test_apply_reconcile_expands_an_alias_whose_anchor_is_not_a_reconciled_entry():
    # Nothing else updates this anchor, so the alias site needs its own seen; editing the
    # anchor instead would silently change the unrelated key that defines it.
    text = "---\nid: d\nshared: &edge {ref: a#x, seen: old}\nderives_from:\n  - *edge\n---\nbody\n"
    expected = (
        "---\n"
        "id: d\n"
        "shared: &edge {ref: a#x, seen: old}\n"
        "derives_from:\n"
        "  - {<<: *edge, seen: newhash}\n"
        "---\n"
        "body\n"
    )

    # NodeMeta forbids the extra key, so this shape only reaches the defensive write path
    # after a concurrent edit; the point is that `shared` keeps its own value.
    out, applied = apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_refuses_source_edits_that_leave_unparseable_frontmatter(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        reconcile_module,
        "_apply_source_edits",
        lambda *_: "id: d\n  bad: [\n",
    )
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nbody\n"

    with pytest.raises(UnreadableDocError, match=r"would leave .* unparseable"):
        apply_reconcile(text, {"a#x": "newhash"}, Path("downstream.md"))


def test_apply_reconcile_no_match_leaves_text_and_reports_nothing():
    # A ref edited away between load and write no longer matches the plan key.
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nbody\n"
    out, applied = apply_reconcile(text, {"a#gone": "newhash"}, Path("downstream.md"))
    assert applied == set()
    assert out == text


def test_apply_reconcile_null_derives_from_is_safe():
    text = "---\nid: d\nderives_from:\n---\nbody\n"
    out, applied = apply_reconcile(text, {"a#x": "h"}, Path("downstream.md"))
    assert applied == set()
    assert out == text


def test_apply_reconcile_unparseable_frontmatter_raises():
    text = "---\nfoo: [1, 2\n---\nbody\n"
    with pytest.raises(UnreadableDocError):
        apply_reconcile(text, {"a#x": "h"}, Path("downstream.md"))


def test_apply_reconcile_non_mapping_frontmatter_raises():
    text = "---\n- just\n- a list\n---\nbody\n"
    with pytest.raises(UnreadableDocError):
        apply_reconcile(text, {"a#x": "h"}, Path("downstream.md"))


def test_apply_reconcile_non_mapping_entry_raises():
    text = "---\nid: d\nderives_from:\n  - plainstring\n---\nbody\n"
    with pytest.raises(UnreadableDocError):
        apply_reconcile(text, {"a#x": "h"}, Path("downstream.md"))


def test_apply_reconcile_non_string_ref_raises():
    text = "---\nid: d\nderives_from:\n  - ref: [a, b]\n    seen: deadbeef\n---\nbody\n"
    with pytest.raises(UnreadableDocError):
        apply_reconcile(text, {"a#x": "h"}, Path("downstream.md"))


def test_reconcile_clears_drift_for_node(lattice_dir: Path):
    project = load_config(None, lattice_dir)
    lat = load_lattice(project)
    plan = reconcile(lat, "pc-design", ref=None, reconcile_all=False)
    _apply_plan(plan)
    # Reload and confirm pc-design no longer drifts.
    relat = load_lattice(load_config(None, lattice_dir))
    pc_states = [s.state for s in check_lattice(relat) if s.source_id == "pc-design"]
    assert pc_states == ["OK", "OK"]


def test_reconcile_preserves_concurrent_body_edit():
    text_initial = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nORIGINAL\n"
    # Simulate a concurrent body edit before the in-place write.
    text_fresh = text_initial.replace("ORIGINAL", "EDITED LATER")
    out, applied = apply_reconcile(text_fresh, {"a#x": "newhash"}, Path("downstream.md"))
    assert "EDITED LATER" in out
    assert "seen: newhash" in out
    assert applied == {"a#x"}


def test_reconcile_node_skips_broken_edge(lattice_dir: Path):
    # gdd's only edge is broken; a node-level reconcile skips it without raising.
    lat = load_lattice(load_config(None, lattice_dir))
    assert reconcile(lat, "gdd", ref=None, reconcile_all=False) == {}


def test_reconcile_ref_targeting_broken_raises(lattice_dir: Path):
    # Aiming --ref directly at a broken edge is still refused.
    lat = load_lattice(load_config(None, lattice_dir))
    with pytest.raises(BrokenRefError):
        reconcile(lat, "gdd", ref="ghost", reconcile_all=False)


def test_reconcile_node_with_stale_and_broken_reconciles_stale(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up {#sec}\nsec body\n", encoding="utf-8")
    (docs / "d.md").write_text(
        "---\nid: d\nderives_from:\n"
        "  - ref: up#sec\n    seen: stalestalestalestalestalestale00\n"
        "  - ref: ghost\n---\n# D\nbody\n",
        encoding="utf-8",
    )
    lat = load_lattice(load_config(None, tmp_path))
    plan = reconcile(lat, "d", ref=None, reconcile_all=False)
    all_refs = _planned_refs(plan)
    assert "up#sec" in all_refs  # the stale edge is reconciled
    assert "ghost" not in all_refs  # the unrelated broken edge is skipped, not raised


def test_reconcile_unknown_id_raises(lattice_dir: Path):
    lat = load_lattice(load_config(None, lattice_dir))
    with pytest.raises(ValidationError) as exc_info:
        reconcile(lat, "does-not-exist", ref=None, reconcile_all=False)
    assert isinstance(exc_info.value, ProjectError)


def test_reconcile_ref_namespaced_matches_stored_ref(lattice_dir: Path):
    lat = load_lattice(load_config(None, lattice_dir))
    plan = reconcile(lat, "pc-design", ref="art-direction#accent", reconcile_all=False)
    assert plan, "plan must be non-empty"
    all_refs = _planned_refs(plan)
    assert "art-direction#accent" in all_refs


def test_reconcile_ref_reuses_resolved_edge_target_ids(monkeypatch, lattice_dir: Path):
    lat = load_lattice(load_config(None, lattice_dir))
    calls = 0

    def counting_parse_ref(ref: str) -> TargetId:
        nonlocal calls
        calls += 1
        return parse_ref(ref)

    monkeypatch.setattr("doc_lattice.reconcile.parse_ref", counting_parse_ref)

    reconcile(lat, "pc-design", ref="art-direction#motion", reconcile_all=False)

    assert calls == 1


def test_reconcile_ref_bare_anchor_no_longer_matches(lattice_dir: Path):
    # A bare anchor ref does not match the file-scoped stored ref: reported, not a silent no-op.
    lat = load_lattice(load_config(None, lattice_dir))
    with pytest.raises(ValidationError):
        reconcile(lat, "pc-design", ref="accent", reconcile_all=False)


def test_reconcile_all_skips_broken_and_ok(lattice_dir: Path):
    lat = load_lattice(load_config(None, lattice_dir))
    # Must not raise despite gdd's BROKEN edge
    plan = reconcile(lat, "", ref=None, reconcile_all=True)  # id ignored under reconcile_all
    all_refs = _planned_refs(plan)
    # pc-design's two drifting edges should be in the plan
    assert "art-direction#accent" in all_refs
    assert "art-direction#motion" in all_refs
    # gdd's broken ghost ref must NOT be in the plan
    assert "ghost" not in all_refs


def test_reconcile_all_memoizes_shared_target_hash(monkeypatch):
    docs = [
        ParsedDoc(Path("up.md"), NodeMeta(id="up"), "# Up {#sec}\nup body\n"),
        *[
            ParsedDoc(
                Path(f"down-{number}.md"),
                NodeMeta(
                    id=f"down-{number}",
                    derives_from=[RawEdge(ref="up#sec", seen="stale")],
                ),
                "downstream body\n",
            )
            for number in range(3)
        ],
    ]
    lattice = build_lattice(docs)
    calls = 0

    def counting_content_hash(content: str) -> str:
        nonlocal calls
        calls += 1
        return content_hash(content)

    monkeypatch.setattr("doc_lattice.resolve.content_hash", counting_content_hash)

    plan = reconcile(lattice, "", ref=None, reconcile_all=True)

    assert set(plan) == {Path(f"down-{number}.md") for number in range(3)}
    assert all("up#sec" in updates for updates in plan.values())
    assert calls == 1

    second_plan = reconcile(lattice, "", ref=None, reconcile_all=True)

    assert second_plan == plan
    assert calls == 2


def test_apply_reconcile_preserves_comments_key_order_and_untargeted_edges():
    # The only mutating command must rewrite just the targeted seen, leaving comments,
    # key order, and a second (untargeted) edge's seen intact. Guards against a regression
    # to a non-round-trip YAML dump.
    text = (
        "---\n"
        "id: d  # the node id\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: oldx\n"
        "  - ref: b#y\n"
        "    seen: oldy\n"
        "tickets:\n"
        "- T-1\n"
        "---\n"
        "# Body\n"
        "keep\n"
    )
    expected = (
        "---\n"
        "id: d  # the node id\n"
        "derives_from:\n"
        "  - ref: a#x\n"
        "    seen: newx\n"
        "  - ref: b#y\n"
        "    seen: oldy\n"
        "tickets:\n"
        "- T-1\n"
        "---\n"
        "# Body\n"
        "keep\n"
    )

    out, applied = apply_reconcile(text, {"a#x": "newx"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}


def test_apply_reconcile_no_change_when_seen_already_matches():
    # A planned ref whose seen already equals the new value is a no-op: not reported,
    # text returned unchanged.
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: same\n---\nbody\n"
    out, applied = apply_reconcile(text, {"a#x": "same"}, Path("downstream.md"))
    assert applied == set()
    assert out == text


def test_reconcile_ref_no_match_raises(lattice_dir: Path):
    # A --ref that names no edge on the node is reported, not a silent exit-0 no-op.
    lat = load_lattice(load_config(None, lattice_dir))
    with pytest.raises(ValidationError):
        reconcile(lat, "pc-design", ref="does-not-exist", reconcile_all=False)


def test_reconcile_node_second_run_skips_already_ok_edges(lattice_dir: Path):
    # After a node is reconciled, a second single-node reconcile plans nothing, since
    # restamping an already-OK edge to the same hash is a no-op.
    project = load_config(None, lattice_dir)
    _apply_plan(reconcile(load_lattice(project), "pc-design", ref=None, reconcile_all=False))
    relat = load_lattice(load_config(None, lattice_dir))
    assert reconcile(relat, "pc-design", ref=None, reconcile_all=False) == {}


def test_reconcile_all_skips_already_ok_edge(lattice_dir: Path):
    # Make pc-design's accent edge OK, leave motion UNRECONCILED, then --all must plan
    # only motion (the OK edge is skipped at reconcile.py's new_seen == seen guard).
    project = load_config(None, lattice_dir)
    _apply_plan(
        reconcile(
            load_lattice(project), "pc-design", ref="art-direction#accent", reconcile_all=False
        )
    )
    relat = load_lattice(load_config(None, lattice_dir))
    plan = reconcile(relat, "", ref=None, reconcile_all=True)  # id ignored under reconcile_all
    refs = _planned_refs(plan)
    assert "art-direction#accent" not in refs  # already OK -> skipped
    assert "art-direction#motion" in refs  # still UNRECONCILED -> planned


def test_apply_reconcile_no_frontmatter_returns_unchanged():
    # No opening fence: a concurrent edit stripped the frontmatter entirely.
    text = "no frontmatter here\njust body\n"
    out, applied = apply_reconcile(text, {"a#x": "h"}, Path("downstream.md"))
    assert out == text
    assert applied == set()


def test_apply_reconcile_empty_frontmatter_returns_unchanged():
    # An empty fence block (yaml.load -> None) is safe, not a crash.
    text = "---\n---\nbody\n"
    out, applied = apply_reconcile(text, {"a#x": "h"}, Path("downstream.md"))
    assert out == text
    assert applied == set()


def test_apply_reconcile_non_list_derives_from_raises():
    # derives_from present but not a list is a distinct error branch from a non-mapping entry.
    text = "---\nid: d\nderives_from: oops\n---\nbody\n"
    with pytest.raises(UnreadableDocError) as exc_info:
        apply_reconcile(text, {"a#x": "h"}, Path("downstream.md"))
    assert exc_info.value.code == "UNREADABLE_DOC"


def test_reconcile_ref_targeting_ok_edge_plans_nothing(lattice_dir: Path):
    # Reconcile accent to OK, then re-target it with --ref: the edge matched so this
    # must return an empty plan, NOT the no-match ValidationError.
    project = load_config(None, lattice_dir)
    _apply_plan(
        reconcile(
            load_lattice(project), "pc-design", ref="art-direction#accent", reconcile_all=False
        )
    )
    relat = load_lattice(load_config(None, lattice_dir))
    assert reconcile(relat, "pc-design", ref="art-direction#accent", reconcile_all=False) == {}


def test_reconcile_all_plans_every_drifting_file(tmp_path: Path):
    # Two distinct drifting downstream nodes must each get their own path key under --all.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up {#sec}\nbody\n", encoding="utf-8")
    (docs / "d1.md").write_text(
        "---\nid: d1\nderives_from:\n  - ref: up#sec\n---\n# D1\nx\n", encoding="utf-8"
    )
    (docs / "d2.md").write_text(
        "---\nid: d2\nderives_from:\n  - ref: up#sec\n---\n# D2\ny\n", encoding="utf-8"
    )
    lat = load_lattice(load_config(None, tmp_path))
    plan = reconcile(lat, "", ref=None, reconcile_all=True)
    assert {p.name for p in plan} == {"d1.md", "d2.md"}
    assert all("up#sec" in updates for updates in plan.values())


def test_reconcile_all_with_ref_filters_without_raising(lattice_dir: Path):
    # --all with --ref narrows edges across all nodes; the raise guards stay suppressed.
    lat = load_lattice(load_config(None, lattice_dir))
    plan = reconcile(lat, "", ref="art-direction#accent", reconcile_all=True)
    refs = _planned_refs(plan)
    assert "art-direction#accent" in refs  # ref-matched edge planned
    assert "art-direction#motion" not in refs  # filtered out by ref, no raise
