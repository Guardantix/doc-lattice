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
from doc_lattice.yaml_boundary import YAML_LOAD_ERRORS, SafeYamlLoader
from doc_lattice.yaml_error_render import format_yaml_error_for_display

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
    assert str(exc.value) == ("unclosed YAML frontmatter in 'broken.md': add a closing '---' fence")


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
        f"frontmatter in 'typo.md' declares {declared} but has no 'id' key, so the file and "
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
        "invalid lattice frontmatter in 'a.md':\n"
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
        "invalid lattice frontmatter in 'a.md':\n"
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
    with pytest.raises(UnreadableDocError, match=r"cannot parse frontmatter in 'doc\.md'"):
        parse_meta(raw_meta, Path("doc.md"))


def test_parse_meta_reports_a_duplicate_key_in_an_ordered_map():
    # An `!!omap` rejects a repeated key with a bare `assert` inside the safe constructor,
    # which is neither a YAMLError nor one of the builtins a tagged scalar raises, so it used
    # to leave this boundary as an uncaught AssertionError.
    with pytest.raises(UnreadableDocError, match=r"cannot parse frontmatter in 'doc\.md'") as exc:
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
    # The message is asserted, not just the type: without it this passes for any FrontmatterError a
    # later validation change might raise on the block, rather than for 1.1 resolution.
    with pytest.raises(FrontmatterError) as exc:
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


# GTX-208 (AD-35): YAML refuses a literal control byte in the source stream but decodes a
# double-quoted escape into a real one, so the escaped spelling is the only way one reaches a
# value and is the spelling every row below is written in. The admitted neighbors sit one code
# point outside a refused range, which is what keeps the rule from reading as "reject anything
# unusual": they are the boundaries a widened predicate would swallow first.
_REFUSED_CONTROLS = (
    ("\\u001b", 0x1B, "esc"),
    ("\\u0000", 0x00, "nul"),
    ("\\t", 0x09, "tab"),
    ("\\n", 0x0A, "newline"),
    ("\\r", 0x0D, "carriage-return"),
    ("\\u001f", 0x1F, "c0-top"),
    ("\\u007f", 0x7F, "delete"),
    ("\\u0080", 0x80, "c1-bottom"),
    ("\\u0085", 0x85, "nel"),
    ("\\u009b", 0x9B, "csi"),
    ("\\u009f", 0x9F, "c1-top"),
)
_ADMITTED_NEIGHBORS = (
    ("\\u0020", 0x20, "space"),
    ("\\u007e", 0x7E, "tilde"),
    ("\\u00a0", 0xA0, "nbsp"),
    ("\\u2028", 0x2028, "line-separator"),
    ("\\ufeff", 0xFEFF, "bom"),
)
# One document per value family, each spelling its own field with the escape under test. The
# five are covered separately because they are validated in three different places: two root
# scalars, a root list element, and two members of a nested model.
_VALUE_FAMILIES = (
    ("id", '---\nid: "node{escape}"\n---\nbody\n'),
    ("title", '---\nid: node\ntitle: "t{escape}"\n---\nbody\n'),
    ("tickets.0", '---\nid: node\ntickets: ["GTX-1{escape}"]\n---\nbody\n'),
    (
        "derives_from.0.ref",
        '---\nid: node\nderives_from:\n  - ref: "up{escape}"\n---\nbody\n',
    ),
    (
        "derives_from.0.seen",
        '---\nid: node\nderives_from:\n  - ref: up\n    seen: "h{escape}"\n---\nbody\n',
    ),
)
_FAMILY_READERS = {
    "id": lambda meta: meta.id,
    "title": lambda meta: meta.title,
    "tickets.0": lambda meta: meta.tickets[0],
    "derives_from.0.ref": lambda meta: meta.derives_from[0].ref,
    "derives_from.0.seen": lambda meta: meta.derives_from[0].seen,
}


def _raw_meta(document: str) -> str:
    """Return a document's frontmatter block, which every row below builds from a template."""
    raw, _ = split_frontmatter(document, Path("a.md"))
    assert raw is not None
    return raw


@pytest.mark.parametrize(
    ("escape", "code"),
    [(row[0], row[1]) for row in _REFUSED_CONTROLS],
    ids=[row[2] for row in _REFUSED_CONTROLS],
)
@pytest.mark.parametrize(
    ("field", "template"), _VALUE_FAMILIES, ids=[row[0] for row in _VALUE_FAMILIES]
)
def test_parse_meta_refuses_a_control_character_in_every_value_family(
    field: str, template: str, escape: str, code: int
):
    with pytest.raises(FrontmatterError) as exc:
        parse_meta(_raw_meta(template.format(escape=escape)), Path("a.md"))

    message = str(exc.value)
    assert "a.md" in message
    # The field location is asserted, not just the exception type: a rule that fired on the
    # wrong member, or a nested location rendered as a bare key, is a failure rather than a pass.
    assert f"  {field}: " in message
    assert f"U+{code:04X}" in message


@pytest.mark.parametrize(
    ("escape", "code"),
    [(row[0], row[1]) for row in _ADMITTED_NEIGHBORS],
    ids=[row[2] for row in _ADMITTED_NEIGHBORS],
)
@pytest.mark.parametrize(
    ("field", "template"), _VALUE_FAMILIES, ids=[row[0] for row in _VALUE_FAMILIES]
)
def test_parse_meta_admits_the_neighbors_of_every_refused_range(
    field: str, template: str, escape: str, code: int
):
    parsed = parse_meta(_raw_meta(template.format(escape=escape)), Path("a.md"))

    assert parsed.disposition == "tracked"
    assert parsed.meta is not None
    assert _FAMILY_READERS[field](parsed.meta).endswith(chr(code))


@pytest.mark.parametrize(
    ("escape", "code"),
    [(row[0], row[1]) for row in _REFUSED_CONTROLS],
    ids=[row[2] for row in _REFUSED_CONTROLS],
)
def test_the_refusal_diagnostic_carries_no_control_character_of_its_own(escape: str, code: int):
    # The point of refusing at validation is that no repo-controlled control byte reaches a
    # person, and the refusal is output like anything else. `validation_render` drops pydantic's
    # echoed input and the message names the code point instead of quoting the value, so both
    # halves have to hold for this to pass. Three fields are spelled at once so the per-error
    # lines are covered and not only the header.
    document = '---\nid: "node{escape}"\ntitle: "t{escape}"\ntickets: ["GTX-1{escape}"]\n---\nb\n'

    with pytest.raises(FrontmatterError) as exc:
        parse_meta(_raw_meta(document.format(escape=escape)), Path("a.md"))

    message = str(exc.value)
    assert not any(
        ord(char) < 0x20 or ord(char) == 0x7F or 0x80 <= ord(char) <= 0x9F
        for char in message.replace("\n", "")
    )
    assert message.count(f"U+{code:04X}") == 3


@pytest.mark.parametrize(
    ("escape", "code"),
    [(row[0], row[1]) for row in _REFUSED_CONTROLS],
    ids=[row[2] for row in _REFUSED_CONTROLS],
)
def test_an_unknown_key_carrying_a_control_character_is_spelled_not_echoed(escape: str, code: int):
    # GTX-208 (AD-35): the value families above are not the whole of what a document controls.
    # A mapping *key* takes the same double-quoted escape, and `extra="forbid"` reports it as the
    # pydantic error location, which is the one repo-controlled part of a location. Refusing the
    # key was never the gap; naming it verbatim was. The nested spelling is covered too, since
    # the location is built part by part and only the last part is the rejected key.
    for template in (
        '---\nid: node\n"bad{escape}key": 1\n---\nbody\n',
        '---\nid: node\nderives_from:\n  - ref: up\n    "bad{escape}key": 1\n---\nbody\n',
    ):
        with pytest.raises(FrontmatterError) as exc:
            parse_meta(_raw_meta(template.format(escape=escape)), Path("a.md"))

        message = str(exc.value)
        # The line count is asserted before the byte check, because a key carrying a line break
        # forges a diagnostic line rather than a color, and stripping newlines to look for
        # control bytes is exactly what would hide that. One header plus one error line is the
        # whole message when the break is spelled instead of taken.
        assert len(message.splitlines()) == 2
        assert not any(
            ord(char) < 0x20 or ord(char) == 0x7F or 0x80 <= ord(char) <= 0x9F
            for char in message.replace("\n", "")
        )
        assert repr(f"bad{chr(code)}key") in message
        assert "Extra inputs are not permitted" in message


def test_a_control_character_is_refused_before_the_id_hash_rule_echoes_the_value():
    # `_id_has_no_hash` quotes the id it rejects, which would print the very byte AD-35 exists
    # to keep out of output. The annotated rule runs ahead of it for that reason, so an id
    # carrying both is reported by the rule that names no value.
    with pytest.raises(FrontmatterError) as exc:
        parse_meta(_raw_meta('---\nid: "a#b\\u001b"\n---\nbody\n'), Path("a.md"))

    message = str(exc.value)
    assert "U+001B" in message
    assert "'#'" not in message
    assert "\x1b" not in message


# GTX-208 (AD-35): which control characters reach a value as a *raw* byte, per scalar spelling.
# "YAML refuses control bytes" is the belief the vector hid behind, and it is wrong for exactly
# one character, so the belief is replaced here by a measured table. A rule written only against
# escaped spellings would admit a literal tab, which is invisible on screen and therefore the
# case an author reaches without meaning to. Each row records the outcome and which layer
# produces it: AD-35's validator, the YAML scanner, or neither, when the byte is read as a line
# break and folded away before a value exists.
_REFUSED_BY_VALIDATOR = "frontmatter-error"
_REFUSED_BY_YAML = "unreadable-doc"
_FOLDED_TO_A_SPACE = "folded"

_RAW_BYTE_SPELLINGS = {
    "double-quoted": 'title: "a{char}b"\n',
    "single-quoted": "title: 'a{char}b'\n",
    "plain": "title: a{char}b\n",
    "literal-block": "title: |-\n  a{char}b\n",
}

# One entry per (character, spelling). Written out rather than derived: the point is to record
# what the pinned pure parser actually does, so a derivation from the same rule the product uses
# would assert nothing.
_RAW_BYTE_REACHABILITY = {
    ("tab", "\t"): {
        "double-quoted": _REFUSED_BY_VALIDATOR,
        "single-quoted": _REFUSED_BY_VALIDATOR,
        "plain": _REFUSED_BY_YAML,
        "literal-block": _REFUSED_BY_VALIDATOR,
    },
    ("esc", "\x1b"): dict.fromkeys(_RAW_BYTE_SPELLINGS, _REFUSED_BY_YAML),
    ("del", "\x7f"): dict.fromkeys(_RAW_BYTE_SPELLINGS, _REFUSED_BY_YAML),
    ("nul", "\x00"): dict.fromkeys(_RAW_BYTE_SPELLINGS, _REFUSED_BY_YAML),
    ("csi", "\x9b"): dict.fromkeys(_RAW_BYTE_SPELLINGS, _REFUSED_BY_YAML),
    ("nel", "\x85"): {
        "double-quoted": _FOLDED_TO_A_SPACE,
        "single-quoted": _FOLDED_TO_A_SPACE,
        "plain": _FOLDED_TO_A_SPACE,
        "literal-block": _REFUSED_BY_YAML,
    },
    ("carriage-return", "\r"): {
        "double-quoted": _FOLDED_TO_A_SPACE,
        "single-quoted": _FOLDED_TO_A_SPACE,
        "plain": _REFUSED_BY_YAML,
        "literal-block": _REFUSED_BY_YAML,
    },
}

_REACHABILITY_ROWS = [
    pytest.param(char, spelling, outcome, id=f"{name}-{spelling}")
    for (name, char), per_spelling in _RAW_BYTE_REACHABILITY.items()
    for spelling, outcome in per_spelling.items()
]


@pytest.mark.parametrize(("char", "spelling", "outcome"), _REACHABILITY_ROWS)
def test_a_raw_control_byte_reaches_a_value_only_as_a_tab(char: str, spelling: str, outcome: str):
    block = "id: doc\n" + _RAW_BYTE_SPELLINGS[spelling].format(char=char)

    if outcome == _REFUSED_BY_VALIDATOR:
        with pytest.raises(FrontmatterError) as exc:
            parse_meta(block, Path("a.md"))
        assert f"U+{ord(char):04X}" in str(exc.value)
        return
    if outcome == _REFUSED_BY_YAML:
        # Refused before validation is reached, so the byte never becomes a value at all. The
        # error type is the distinction: this is an unreadable document, not an invalid one.
        with pytest.raises(UnreadableDocError):
            parse_meta(block, Path("a.md"))
        return

    # Read as a line break: the scalar spans two lines and folds, so no control character
    # survives into the value and AD-35 has nothing to refuse.
    parsed = parse_meta(block, Path("a.md"))
    assert parsed.meta is not None
    assert parsed.meta.title == "a b"


def test_the_tab_is_the_only_raw_byte_the_validator_ever_sees():
    # The claim README and CHANGELOG make to an upgrading adopter, stated once as a property of
    # the table above rather than left implicit across its rows. If a parser change ever lets a
    # second raw byte through to validation, the migration guidance is wrong and this fails.
    reaching_validation = {
        name
        for (name, _char), per_spelling in _RAW_BYTE_REACHABILITY.items()
        if _REFUSED_BY_VALIDATOR in per_spelling.values()
    }

    assert reaching_validation == {"tab"}


# GTX-208 (AD-35): which block-scalar spellings survive the rule, per style and chomping mode.
# The migration guidance rests on this table, and its first version was wrong in one cell: it
# read the rule as being about chomping alone, so it told an adopter that `|-` was safe. Chomping
# governs only the break at the *end* of a block; a literal style keeps the breaks *between* its
# lines whatever the chomping is, so a multi-line `|-` constructs an interior newline and is
# refused. Only the folded styles join their lines with a space. Recorded as a measured table for
# the same reason the raw-byte one above is: the belief is what shipped the wrong advice.
_BLOCK_SCALAR_SPELLINGS = {
    "literal-clip-one-line": ("|", ["up"], None),
    "literal-strip-one-line": ("|-", ["up"], "up"),
    "literal-keep-one-line": ("|+", ["up"], None),
    "literal-clip-two-lines": ("|", ["up", "down"], None),
    "literal-strip-two-lines": ("|-", ["up", "down"], None),
    "folded-clip-two-lines": (">", ["up", "down"], None),
    "folded-strip-one-line": (">-", ["up"], "up"),
    "folded-strip-two-lines": (">-", ["up", "down"], "up down"),
    "folded-keep-two-lines": (">+", ["up", "down"], None),
}


@pytest.mark.parametrize(
    ("header", "lines", "constructed"),
    [pytest.param(*row, id=name) for name, row in _BLOCK_SCALAR_SPELLINGS.items()],
)
def test_only_a_single_line_literal_or_a_folded_block_survives_the_control_rule(
    header: str, lines: list[str], constructed: str | None
):
    body = "".join(f"  {line}\n" for line in lines)
    block = f"id: doc\ntitle: {header}\n{body}"

    if constructed is None:
        with pytest.raises(FrontmatterError) as exc:
            parse_meta(block, Path("a.md"))
        assert "U+000A" in str(exc.value)
        return

    parsed = parse_meta(block, Path("a.md"))
    assert parsed.meta is not None
    assert parsed.meta.title == constructed


def test_the_folded_styles_are_the_only_ones_that_survive_across_lines():
    # The sentence README and CHANGELOG both give an adopter, stated once as a property of the
    # table above. If a dependency change ever makes a literal block join its lines, or stops a
    # folded one from doing so, the migration advice is wrong and this fails.
    surviving_multi_line = {
        header
        for header, lines, constructed in _BLOCK_SCALAR_SPELLINGS.values()
        if len(lines) > 1 and constructed is not None
    }

    assert surviving_multi_line == {">-"}


# GTX-219 (AD-37): a duplicate key aborts the load before any value rule runs, and `ruamel`'s
# constructor error echoes the offending key and both of its values back at the reader. The two
# templates are the two halves that echo: the key is reported once, the values twice.
_DUPLICATE_KEY_ECHOES = (
    ("key", 'id: doc\n"k{escape}": 1\n"k{escape}": 2\n'),
    ("value", 'id: doc\nk: "v{escape}A"\nk: "v{escape}B"\n'),
)


@pytest.mark.parametrize(
    "escape",
    [row[0] for row in _REFUSED_CONTROLS],
    ids=[row[2] for row in _REFUSED_CONTROLS],
)
@pytest.mark.parametrize(
    "template",
    [row[1] for row in _DUPLICATE_KEY_ECHOES],
    ids=[row[0] for row in _DUPLICATE_KEY_ECHOES],
)
def test_a_load_failure_echoing_a_control_character_is_spelled_not_printed_raw(
    template: str, escape: str
):
    # The vector AD-35 could not reach: the value half defeats its guarantee outright, because
    # the value never becomes a node and the refusal never runs. Both halves are asserted over
    # the whole refused range rather than the ESC the vector was reported for, since the message
    # is third-party text and nothing about it privileges one code point.
    with pytest.raises(UnreadableDocError) as exc:
        parse_meta(template.format(escape=escape), Path("a.md"))

    message = str(exc.value)
    assert "cannot parse frontmatter in 'a.md'" in message
    # The whole message is scanned, line breaks included: a spelling that kept the message's own
    # breaks would let an echoed value forge a diagnostic line, and stripping them before looking
    # for control bytes is what would hide exactly that.
    assert not any(
        ord(char) < 0x20 or ord(char) == 0x7F or 0x80 <= ord(char) <= 0x9F for char in message
    )
    assert len(message.splitlines()) == 1


def test_the_load_failure_detail_is_the_display_spelling_of_the_caught_exception():
    # The readability cost, pinned as a relation rather than as a literal: `ruamel`'s wording
    # differs across the releases and accelerator cells CI runs, so a fixed expected string
    # would pass in one and fail in another. What is fixed is that the detail is exactly the
    # spelling of whatever was caught, and that an ordinary syntax error therefore arrives as
    # one quoted line whose caret art no longer points at anything while its `line: N, column: M`
    # coordinates survive.
    block = "id: [unclosed\n"
    with pytest.raises(YAML_LOAD_ERRORS) as caught:
        SafeYamlLoader(parser="pure").load(block)

    with pytest.raises(UnreadableDocError) as exc:
        parse_meta(block, Path("a.md"))

    message = str(exc.value)
    detail = format_yaml_error_for_display(caught.value)
    assert message == f"cannot parse frontmatter in 'a.md': {detail}"
    assert message.endswith(repr(str(caught.value)))
    assert "\\n" in message
    assert "line: 1" in message


# ---------------------------------------------------------------------------
# GTX-204: the failing document is carried as structured data, not only inside the message.


def test_schema_failure_carries_the_document_it_names():
    # The `NodeMeta.model_validate` raise site. Asserting the attribute rather than parsing the
    # path back out of the message is the whole point of the change: a renderer that wants to
    # annotate the file cannot re-derive it from a formatted diagnostic.
    source = Path("docs/a.md")

    with pytest.raises(FrontmatterError) as exc:
        parse_meta("id: x\nlayer: bogus\n", source)

    assert exc.value.source == source


def test_id_less_lattice_intent_failure_carries_the_document_it_names():
    # The second `FrontmatterError` raise site, which formats its own message rather than going
    # through `format_validation_error`, so it carries the path independently.
    source = Path("docs/typo.md")

    with pytest.raises(FrontmatterError) as exc:
        parse_meta("idd: x\nderives_from: []\n", source)

    assert exc.value.source == source


def test_an_unparseable_frontmatter_block_carries_the_document_it_names():
    source = Path("docs/a.md")

    with pytest.raises(UnreadableDocError) as exc:
        parse_meta("id: [unclosed\n", source)

    assert exc.value.source == source


def test_an_unclosed_fence_carries_the_document_it_names():
    source = Path("docs/a.md")

    with pytest.raises(UnreadableDocError) as exc:
        split_frontmatter_parts("---\nid: x\n# no closing fence\n", source)

    assert exc.value.source == source


def test_the_carried_document_is_the_spelling_the_caller_passed():
    # Discovery keeps each document's unresolved path as its identity, and annotation-root
    # containment is lexical, so a raise site that resolved or normalized here would move the
    # annotation to a different base than the drift findings use.
    source = Path("docs/../docs/a.md")

    with pytest.raises(FrontmatterError) as exc:
        parse_meta("id: x\nlayer: bogus\n", source)

    assert exc.value.source == source


def test_fence_split_reports_its_kind_and_inner_yaml_offsets():
    text = "﻿---   \nid: pc\n---  \n# Body\n"

    parts = split_frontmatter_parts(text, Path("a.md"))

    assert parts is not None
    assert parts.kind == "fence"
    assert text[parts.meta_start : parts.meta_end] == parts.raw_meta
    assert parts.raw_meta == "id: pc\n"


def test_fence_split_offsets_are_empty_for_an_empty_block():
    parts = split_frontmatter_parts("---\n---\n# Body\n", Path("a.md"))

    assert parts is not None
    assert parts.raw_meta == ""
    assert parts.meta_start == parts.meta_end
