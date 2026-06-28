"""Tests for frontmatter parsing."""

from pathlib import Path

import pytest

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


def test_parse_meta_returns_node():
    meta = parse_meta("id: pc\ntitle: PC\n", Path("a.md"))
    assert meta is not None
    assert meta.id == "pc"


def test_parse_meta_none_without_id():
    assert parse_meta("title: no id here\n", Path("a.md")) is None
    assert parse_meta(None, Path("a.md")) is None


def test_parse_meta_unknown_key_raises():
    with pytest.raises(ConfigError):
        parse_meta("id: x\nbogus: 1\n", Path("a.md"))


def test_parse_meta_bad_yaml_raises():
    with pytest.raises(UnreadableDocError):
        parse_meta("id: [unclosed\n", Path("a.md"))
