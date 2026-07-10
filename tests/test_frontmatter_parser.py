"""Tests for frontmatter parsing."""

from pathlib import Path

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

import game_lattice.frontmatter_parser as frontmatter_parser_module
from game_lattice.error_types import ConfigError, UnreadableDocError
from game_lattice.frontmatter_parser import parse_meta, split_frontmatter

DOC = "---\nid: pc\ntitle: PC\n---\n# Body\ntext\n"


def test_split_frontmatter_separates_meta_and_body():
    raw, body = split_frontmatter(DOC)
    assert raw == "id: pc\ntitle: PC\n"
    assert body == "# Body\ntext\n"


def test_split_frontmatter_none_when_absent():
    raw, body = split_frontmatter("# No frontmatter\n")
    assert raw is None
    assert body == "# No frontmatter\n"


def test_split_frontmatter_tolerates_bom():
    raw, _body = split_frontmatter("﻿---\nid: x\n---\nbody\n")
    assert raw == "id: x\n"


def test_split_frontmatter_bom_preserves_body():
    raw, body = split_frontmatter("﻿---\nid: x\n---\nbody\n")
    assert raw == "id: x\n"
    assert body == "body\n"


def test_split_frontmatter_bom_without_fence_returns_original():
    text = "﻿# No frontmatter\n"
    raw, body = split_frontmatter(text)
    assert raw is None
    assert body == text  # original text (BOM still present) returned unchanged


def test_split_frontmatter_empty_block_returns_empty_string():
    raw, body = split_frontmatter("---\n---\n# Body\n")
    assert raw == ""  # empty string, NOT None: an empty fence differs from no fence
    assert body == "# Body\n"


def test_split_frontmatter_unclosed_fence_returns_none():
    text = "---\nid: x\nno closing fence\n"
    raw, body = split_frontmatter(text)
    assert raw is None
    assert body == text  # original text returned unchanged when no closing fence


def test_split_frontmatter_detects_crlf_fences():
    raw, _body = split_frontmatter("---\r\nid: x\r\n---\r\nbody\r\n")
    assert raw is not None
    meta = parse_meta(raw, Path("a.md"))
    assert meta is not None
    assert meta.id == "x"


@given(st.text())
def test_split_frontmatter_identity_when_no_opening_fence(text):
    first_line = text.lstrip("﻿").split("\n", 1)[0]
    assume(first_line.strip() != "---")
    raw, body = split_frontmatter(text)
    assert raw is None
    assert body == text


def test_parse_meta_returns_node():
    meta = parse_meta("id: pc\ntitle: PC\n", Path("a.md"))
    assert meta is not None
    assert meta.id == "pc"


def test_parse_meta_reuses_safe_yaml_loader(monkeypatch):
    raw_documents = ["id: first\n", "id: second\n"]
    original_yaml = frontmatter_parser_module._YAML
    calls: list[str] = []

    class TrackingYAML:
        def load(self, raw_meta: str):
            calls.append(raw_meta)
            return original_yaml.load(raw_meta)

    monkeypatch.setattr(frontmatter_parser_module, "_YAML", TrackingYAML())

    metas = [parse_meta(raw, Path(f"{index}.md")) for index, raw in enumerate(raw_documents)]

    assert [meta.id for meta in metas if meta is not None] == ["first", "second"]
    assert calls == raw_documents


def test_parse_meta_maps_all_fields():
    raw = (
        "id: pc-design\ntitle: PC Design\nlayer: design\nauthority: derived\n"
        "derives_from:\n  - ref: art-direction#accent\n    seen: abc\n  - ref: motion\n"
        "tickets: [PC-1, PC-2]\n"
    )
    meta = parse_meta(raw, Path("pc-design.md"))
    assert meta is not None
    assert meta.id == "pc-design"
    assert meta.title == "PC Design"
    assert meta.layer == "design"
    assert meta.authority == "derived"
    assert [e.ref for e in meta.derives_from] == ["art-direction#accent", "motion"]
    assert meta.derives_from[0].seen == "abc"
    assert meta.derives_from[1].seen is None  # seen defaults to None
    assert meta.tickets == ["PC-1", "PC-2"]


def test_parse_meta_none_without_id():
    assert parse_meta("title: no id here\n", Path("a.md")) is None
    assert parse_meta(None, Path("a.md")) is None


@pytest.mark.parametrize("raw", ["", "just a scalar\n", "- a\n- b\n"])
def test_parse_meta_none_for_non_mapping_yaml(raw):
    # YAML that parses to None / scalar / list is not a mapping -> not a lattice node
    assert parse_meta(raw, Path("a.md")) is None


def test_parse_meta_unknown_key_raises():
    with pytest.raises(ConfigError):
        parse_meta("id: x\nbogus: 1\n", Path("a.md"))


@pytest.mark.parametrize(
    "raw",
    [
        "id: x\nlayer: bogus\n",  # not in Layer literal
        "id: x\nauthority: maybe\n",  # not in Authority literal
        "id: 123\n",  # strict mode: id must be str
        "id: x\nderives_from:\n  - ref: a\n    bogus: 1\n",  # RawEdge extra=forbid
    ],
)
def test_parse_meta_invalid_value_raises_config_error(raw):
    with pytest.raises(ConfigError) as exc:
        parse_meta(raw, Path("a.md"))
    assert exc.value.code == "CONFIG_ERROR"
    assert "a.md" in str(exc.value)  # message names the source file


def test_parse_meta_bad_yaml_raises():
    with pytest.raises(UnreadableDocError):
        parse_meta("id: [unclosed\n", Path("a.md"))


def test_parse_meta_bad_yaml_carries_code_and_names_file():
    with pytest.raises(UnreadableDocError) as exc:
        parse_meta("id: [unclosed\n", Path("a.md"))
    assert exc.value.code == "UNREADABLE_DOC"
    assert "a.md" in str(exc.value)


def test_safe_yaml_loader_resets_version_after_malformed_frontmatter():
    with pytest.raises(UnreadableDocError):
        parse_meta("%YAML 1.1\nid: [unclosed\n", Path("broken.md"))

    meta = parse_meta("id: on\n", Path("next.md"))

    assert meta is not None
    assert meta.id == "on"
