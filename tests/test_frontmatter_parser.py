"""Tests for frontmatter parsing."""

import warnings
from pathlib import Path

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from ruamel.yaml.error import ReusedAnchorWarning

import doc_lattice.frontmatter_parser as frontmatter_parser_module
from doc_lattice.constants import LATTICE_INTENT_KEYS
from doc_lattice.error_types import FrontmatterError, UnreadableDocError
from doc_lattice.frontmatter_parser import (
    parse_meta,
    split_frontmatter,
    split_frontmatter_parts,
)
from doc_lattice.model import NodeMeta, RawEdge

DOC = "---\nid: pc\ntitle: PC\n---\n# Body\ntext\n"


def test_split_frontmatter_separates_meta_and_body():
    raw, body = split_frontmatter(DOC, Path("a.md"))
    assert raw == "id: pc\ntitle: PC\n"
    assert body == "# Body\ntext\n"


def test_split_frontmatter_parts_keeps_every_piece_it_read():
    text = "﻿---   \nid: pc\n---  \n# Body\n"

    parts = split_frontmatter_parts(text, Path("a.md"))

    assert parts is not None
    assert parts.prefix == "﻿"
    assert parts.open_fence == "---   "
    assert parts.raw_meta == "id: pc\n"
    assert parts.close_fence == "---  "
    assert parts.close_fence_newline == "\n"
    assert parts.body == "# Body\n"
    # Every piece together is the document it was given, byte for byte.
    assert (
        parts.prefix
        + parts.open_fence
        + "\n"
        + parts.raw_meta
        + parts.close_fence
        + parts.close_fence_newline
        + parts.body
    ) == text


@pytest.mark.parametrize(
    ("text", "newline"),
    [("---\nid: pc\n---", ""), ("---\nid: pc\n---\n", "\n")],
)
def test_split_frontmatter_parts_records_whether_the_closing_fence_ends_the_file(
    text: str, newline: str
):
    parts = split_frontmatter_parts(text, Path("a.md"))

    assert parts is not None
    assert parts.close_fence_newline == newline
    assert parts.body == ""


def test_split_frontmatter_parts_none_when_absent():
    assert split_frontmatter_parts("# No frontmatter\n", Path("a.md")) is None


def test_split_frontmatter_none_when_absent():
    raw, body = split_frontmatter("# No frontmatter\n", Path("a.md"))
    assert raw is None
    assert body == "# No frontmatter\n"


def test_split_frontmatter_tolerates_bom():
    raw, _body = split_frontmatter("﻿---\nid: x\n---\nbody\n", Path("a.md"))
    assert raw == "id: x\n"


def test_split_frontmatter_bom_preserves_body():
    raw, body = split_frontmatter("﻿---\nid: x\n---\nbody\n", Path("a.md"))
    assert raw == "id: x\n"
    assert body == "body\n"


def test_split_frontmatter_bom_without_fence_returns_original():
    text = "﻿# No frontmatter\n"
    raw, body = split_frontmatter(text, Path("a.md"))
    assert raw is None
    assert body == text  # original text (BOM still present) returned unchanged


def test_split_frontmatter_empty_block_returns_empty_string():
    raw, body = split_frontmatter("---\n---\n# Body\n", Path("a.md"))
    assert raw == ""  # empty string, NOT None: an empty fence differs from no fence
    assert body == "# Body\n"


def test_split_frontmatter_unclosed_fence_raises_source_naming_error():
    text = "---\nid: x\nno closing fence\n"

    with pytest.raises(UnreadableDocError) as exc:
        split_frontmatter(text, Path("broken.md"))

    assert exc.value.code == "UNREADABLE_DOC"
    assert str(exc.value) == ("unclosed YAML frontmatter in broken.md: add a closing '---' fence")


def test_split_frontmatter_detects_crlf_fences():
    raw, _body = split_frontmatter("---\r\nid: x\r\n---\r\nbody\r\n", Path("a.md"))
    assert raw is not None
    parsed = parse_meta(raw, Path("a.md"))
    assert parsed.meta is not None
    assert parsed.meta.id == "x"


@given(st.text())
def test_split_frontmatter_identity_when_no_opening_fence(text):
    first_line = text.lstrip("﻿").split("\n", 1)[0]
    assume(first_line.strip() != "---")
    raw, body = split_frontmatter(text, Path("a.md"))
    assert raw is None
    assert body == text


def test_parse_meta_returns_node():
    parsed = parse_meta("id: pc\ntitle: PC\n", Path("a.md"))
    assert parsed.disposition == "tracked"
    assert parsed.meta is not None
    assert parsed.meta.id == "pc"


def test_parse_meta_reuses_safe_yaml_loader(monkeypatch):
    raw_documents = ["id: first\n", "id: second\n"]
    original_loader = frontmatter_parser_module._LOADER
    calls: list[str] = []

    class TrackingLoader:
        def load(self, raw_meta: str):
            calls.append(raw_meta)
            return original_loader.load(raw_meta)

    monkeypatch.setattr(frontmatter_parser_module, "_LOADER", TrackingLoader())

    parsed = [parse_meta(raw, Path(f"{index}.md")) for index, raw in enumerate(raw_documents)]

    assert [p.meta.id for p in parsed if p.meta is not None] == ["first", "second"]
    assert calls == raw_documents


def test_parse_meta_maps_all_fields():
    raw = (
        "id: pc-design\ntitle: PC Design\nlayer: design\nauthority: derived\n"
        "derives_from:\n  - ref: art-direction#accent\n    seen: abc\n  - ref: motion\n"
        "tickets: [PC-1, PC-2]\n"
    )
    meta = parse_meta(raw, Path("pc-design.md")).meta
    assert meta is not None
    assert meta.id == "pc-design"
    assert meta.title == "PC Design"
    assert meta.layer == "design"
    assert meta.authority == "derived"
    assert [e.ref for e in meta.derives_from] == ["art-direction#accent", "motion"]
    assert meta.derives_from[0].seen == "abc"
    assert meta.derives_from[1].seen is None  # seen defaults to None
    assert meta.tickets == ["PC-1", "PC-2"]


@pytest.mark.parametrize(
    "raw",
    [
        "title: no id here\n",
        "name: some-skill\ndescription: non-lattice frontmatter\n",
        "layer: design\n",  # `layer` describes a doc without wiring it into the graph
        "title: t\nlayer: technical\n",
    ],
)
def test_parse_meta_id_less_metadata_is_a_reportable_skip(raw):
    # A fenced block with no `id` and no lattice intent is metadata this engine does not own.
    # It is skipped, but the skip is named so it can be reported rather than vanishing.
    parsed = parse_meta(raw, Path("a.md"))
    assert parsed.meta is None
    assert parsed.disposition == "id-less"


def test_parse_meta_no_fence_is_untracked_prose():
    parsed = parse_meta(None, Path("a.md"))
    assert parsed.meta is None
    assert parsed.disposition == "untracked"


@pytest.mark.parametrize("raw", ["", "just a scalar\n", "- a\n- b\n"])
def test_parse_meta_non_mapping_yaml_is_untracked_not_a_skip(raw):
    # YAML that parses to None / scalar / list declares no keys at all, so it is the same
    # untracked prose as a file with no fence. Grading it as a skip would report every
    # document that merely opens with a thematic break.
    parsed = parse_meta(raw, Path("a.md"))
    assert parsed.meta is None
    assert parsed.disposition == "untracked"


@pytest.mark.parametrize(
    ("raw", "declared"),
    [
        ("idd: down\nderives_from:\n  - ref: up\n", "'derives_from'"),
        ("id_: down\nauthority: binding\n", "'authority'"),
        ("title: t\ntickets: [PC-1]\n", "'tickets'"),
        # Presence, not truth: an empty list or a null still declares lattice intent, and a
        # truthiness check would let all three of these back through as a silent skip.
        ("derives_from: []\n", "'derives_from'"),
        ("tickets: []\n", "'tickets'"),
        ("authority:\n", "'authority'"),
        # Every declared key is named, in a stable sorted order.
        (
            "idd: x\ntickets: []\nderives_from: []\nauthority:\n",
            "'authority', 'derives_from', 'tickets'",
        ),
    ],
)
def test_parse_meta_id_less_lattice_intent_raises_naming_file_and_keys(raw: str, declared: str):
    with pytest.raises(FrontmatterError) as exc:
        parse_meta(raw, Path("typo.md"))

    assert exc.value.code == "FRONTMATTER_ERROR"
    assert str(exc.value) == (
        f"frontmatter in typo.md declares {declared} but has no 'id' key, so the file and "
        "every edge it declares would be dropped from the lattice; add an 'id' (check it for "
        "a typo) or remove the lattice keys"
    )


def test_parse_meta_intent_keys_are_an_exact_set_not_every_node_meta_field():
    # NodeMeta also recognizes `title` and `layer`. Deriving the hard-error tier mechanically
    # from its fields would pull those in and turn ordinary non-lattice frontmatter into an
    # exit 2, so the trigger set is declared rather than computed.
    assert {"authority", "derives_from", "tickets"} == LATTICE_INTENT_KEYS
    assert {"layer", "title"} == set(NodeMeta.model_fields) - LATTICE_INTENT_KEYS - {"id"}


def test_parse_meta_unknown_key_raises():
    with pytest.raises(FrontmatterError):
        parse_meta("id: x\nbogus: 1\n", Path("a.md"))


def test_parse_meta_unknown_key_lists_the_accepted_keys():
    # The frontmatter boundary renders through the same curated formatter as the config
    # boundary, so an unknown key here gets the same help a config key does.
    with pytest.raises(FrontmatterError) as exc:
        parse_meta("id: x\nbogus: 1\n", Path("a.md"))

    accepted = ", ".join(sorted(NodeMeta.model_fields))
    assert str(exc.value) == (
        "invalid lattice frontmatter in a.md:\n"
        f"  bogus: Extra inputs are not permitted (accepted keys: {accepted})"
    )


def test_parse_meta_unknown_key_in_an_edge_lists_the_edge_keys_not_the_node_keys():
    # derives_from holds RawEdge, so a key rejected inside one is answered by RawEdge's fields.
    # Offering NodeMeta's would name keys that are invalid exactly where the user is editing.
    with pytest.raises(FrontmatterError) as exc:
        parse_meta("id: x\nderives_from:\n  - ref: a\n    bogus: 1\n", Path("a.md"))

    message = str(exc.value)
    assert "  derives_from.0.bogus: " in message
    assert f"accepted keys: {', '.join(sorted(RawEdge.model_fields))}" in message
    assert "authority" not in message


def test_parse_meta_error_omits_pydantic_url_and_echoed_input():
    # str(ValidationError) leaks a versioned docs URL and echoes the offending value back; the
    # diagnostic contract is owned by validation_render, on both load boundaries alike.
    with pytest.raises(FrontmatterError) as exc:
        parse_meta("id: 123\n", Path("a.md"))

    message = str(exc.value)
    assert "pydantic.dev" not in message
    assert "input_value" not in message
    assert "[type=" not in message


def test_parse_meta_value_error_reads_as_the_domain_wrote_it():
    # A validator that raises ValueError is prefixed "Value error, " by pydantic; that is
    # boilerplate, not part of the sentence the domain authored.
    with pytest.raises(FrontmatterError) as exc:
        parse_meta("id: 'a#b'\n", Path("a.md"))

    assert str(exc.value) == (
        "invalid lattice frontmatter in a.md:\n"
        "  id: node id 'a#b' must not contain '#'; "
        "'#' separates a file id from a section anchor"
    )
    assert "Value error," not in str(exc.value)


@pytest.mark.parametrize(
    "raw",
    [
        "id: x\nlayer: bogus\n",  # not in Layer literal
        "id: x\nauthority: maybe\n",  # not in Authority literal
        "id: 123\n",  # strict mode: id must be str
        "id: x\nderives_from:\n  - ref: a\n    bogus: 1\n",  # RawEdge extra=forbid
    ],
)
def test_parse_meta_invalid_value_raises_frontmatter_error(raw):
    with pytest.raises(FrontmatterError) as exc:
        parse_meta(raw, Path("a.md"))
    assert exc.value.code == "FRONTMATTER_ERROR"
    assert "a.md" in str(exc.value)  # message names the source file


def test_parse_meta_bad_yaml_raises():
    with pytest.raises(UnreadableDocError):
        parse_meta("id: [unclosed\n", Path("a.md"))


@pytest.mark.parametrize("raw_meta", ["id: d\ncount: !!int oops\n", "id: d\nflag: !!bool maybe\n"])
def test_parse_meta_reports_a_tagged_scalar_its_type_cannot_build(raw_meta: str):
    # A safe constructor asked for a type it cannot build from a scalar raises the builtin
    # that construction failed with rather than a YAMLError, so catching only that family
    # let a bare ValueError out of the boundary and print as an internal error.
    with pytest.raises(UnreadableDocError, match=r"cannot parse frontmatter in doc\.md"):
        parse_meta(raw_meta, Path("doc.md"))


def test_parse_meta_reports_a_duplicate_key_in_an_ordered_map():
    # An `!!omap` rejects a repeated key with a bare `assert` inside the safe constructor,
    # which is neither a YAMLError nor one of the builtins a tagged scalar raises, so it used
    # to leave this boundary as an uncaught AssertionError.
    with pytest.raises(UnreadableDocError, match=r"cannot parse frontmatter in doc\.md") as exc:
        parse_meta("id: d\nextra: !!omap\n- a: 1\n- a: 2\n", Path("doc.md"))

    assert exc.value.code == "UNREADABLE_DOC"


def test_parse_meta_bad_yaml_carries_code_and_names_file():
    with pytest.raises(UnreadableDocError) as exc:
        parse_meta("id: [unclosed\n", Path("a.md"))
    assert exc.value.code == "UNREADABLE_DOC"
    assert "a.md" in str(exc.value)


def test_safe_yaml_loader_resets_version_after_malformed_frontmatter():
    with pytest.raises(UnreadableDocError):
        parse_meta("%YAML 1.1\nid: [unclosed\n", Path("broken.md"))

    meta = parse_meta("id: on\n", Path("next.md")).meta

    assert meta is not None
    assert meta.id == "on"


# A frontmatter block defining one anchor name twice. Every alias reads the nearest definition
# above it, so `*target` rebinds to the second `&target` and the third entry's ref is "second".
# The strict load is pinned to the pure Python parser (AD-33), so this block is tracked whether
# or not the optional `ruamel.yaml.clib` accelerator is installed; the `yaml-compatibility` CI
# leg runs both answers and neither one is skipped or routed around here.
REUSED_ANCHOR_FRONTMATTER = (
    "id: pc\nderives_from:\n  - ref: &target first\n  - ref: &target second\n  - ref: *target\n"
)


def test_parse_meta_tracks_a_reused_anchor_name_under_either_installed_parser():
    # The verdict is the whole point: before the pure pin this block was tracked without the
    # accelerator and refused as a duplicate anchor with it, so `check` disagreed with itself
    # across two environments holding the same file. Asserted unconditionally, on both legs.
    parsed = parse_meta(REUSED_ANCHOR_FRONTMATTER, Path("a.md"))

    assert parsed.disposition == "tracked"
    assert parsed.meta is not None
    assert [edge.ref for edge in parsed.meta.derives_from] == ["first", "second", "second"]
    assert parsed.reused_anchors is True


def test_parse_meta_captures_the_reused_anchor_warning_rather_than_letting_it_escape():
    # ruamel raises the warning from inside its own composer, so it names no document and does
    # not run at all on a warm cache. AD-29 requires a load-emitted diagnostic to be derivable
    # from a cache entry and rendered at one shared site, so the parse returns the fact and
    # `orchestrate` reports it against the discovered path.
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        parse_meta(REUSED_ANCHOR_FRONTMATTER, Path("a.md"))

    assert [w for w in captured if issubclass(w.category, ReusedAnchorWarning)] == []


def test_parse_meta_leaves_every_other_warning_alone(monkeypatch):
    # Only the one category is intercepted. A warning raised for any other reason during the
    # load still reaches the caller. Substituting the whole loader rather than patching its
    # `load` attribute, since `SafeYamlLoader` is slotted and its attributes are read-only.
    class WarningLoader:
        def load(self, _text: str) -> dict[str, str]:
            warnings.warn("unrelated", UserWarning, stacklevel=1)
            return {"id": "pc"}

    monkeypatch.setattr(frontmatter_parser_module, "_LOADER", WarningLoader())

    with pytest.warns(UserWarning, match="unrelated"):
        parsed = parse_meta("id: pc\n", Path("a.md"))

    assert parsed.disposition == "tracked"
    assert parsed.reused_anchors is False


def test_parse_meta_resolves_a_block_under_the_yaml_version_it_declares():
    # The other spelling the pure pin settles. AD-31 layer 2a declares a `%YAML` directive
    # supported alongside a document start that does not strip to `---`, and the reread inside
    # `apply_reconcile` has always honored it, but the C parser ignored the directive outright,
    # so the strict read resolved the block under 1.2 wherever the accelerator was installed.
    # Under 1.1 an unquoted `on` is a boolean, so the two reads disagreed about the same bytes.
    quoted = parse_meta("%YAML 1.1\n--- !!map\nid: 'on'\n", Path("quoted.md"))
    assert quoted.meta is not None
    assert quoted.meta.id == "on"

    # The same block unquoted resolves to a boolean and fails validation, which is the
    # user-visible half of settling the disagreement and is why CHANGELOG.md calls it out.
    # The message is asserted, not just the type: without it this passes for any ConfigError a
    # later validation change might raise on the block, rather than for 1.1 resolution.
    with pytest.raises(ConfigError) as exc:
        parse_meta("%YAML 1.1\n--- !!map\nid: on\n", Path("directive.md"))

    assert "directive.md" in str(exc.value)
    assert "id" in str(exc.value)


def test_parse_meta_tracks_a_reused_anchor_name_after_a_directive_reset():
    # A `%YAML` directive makes `SafeYamlLoader` discard its underlying loader and build a
    # replacement, which is a second construction site the parser choice has to reach. Pinning
    # only the first one would restore the environment-dependent verdict for every document
    # read after a directive rather than for none.
    parse_meta("%YAML 1.1\n--- !!map\nid: first\n", Path("directive.md"))

    parsed = parse_meta(REUSED_ANCHOR_FRONTMATTER, Path("a.md"))

    assert parsed.disposition == "tracked"
    assert parsed.meta is not None
    assert [edge.ref for edge in parsed.meta.derives_from] == ["first", "second", "second"]
    assert parsed.reused_anchors is True
