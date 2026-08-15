"""Tests for the shared ruamel safe-load boundary."""

import pytest
from ruamel.yaml.error import YAMLError

from doc_lattice.yaml_boundary import YAML_LOAD_ERRORS, SafeYamlLoader


def test_load_returns_the_constructed_document():
    assert SafeYamlLoader().load("a: 1\nb: [x, y]\n") == {"a": 1, "b": ["x", "y"]}


def test_load_returns_none_for_an_empty_document():
    # The None is deliberately not normalized here: config owns the `None -> {}` fallback,
    # and frontmatter treats a non-dict as "not a lattice node", so the two callers want
    # different things from an empty file.
    assert SafeYamlLoader().load("") is None


def test_load_resets_the_yaml_version_between_documents():
    loader = SafeYamlLoader()

    # YAML 1.1 resolves an unquoted `on` to True, 1.2 keeps it the string "on", so a version
    # that leaked forward from the directive would turn the second load's value into True.
    # Only the second load is asserted. Whether the first document itself resolves under 1.1
    # depends on which parser implementation is installed: the optional `ruamel.yaml.clib`
    # accelerator skips the directive and resolver state entirely (AD-26), and the
    # `yaml-compatibility` CI leg runs the suite with it present. The no-leak property holds
    # under both, which is the property this helper is responsible for.
    loader.load("%YAML 1.1\n---\nkey: on\n")

    assert loader.load("key: on\n") == {"key": "on"}


def test_load_resets_the_yaml_version_after_a_failed_parse():
    loader = SafeYamlLoader()

    # A directive can update the parser's version even when the document it heads fails to
    # parse, so the reset has to survive the exception rather than run after a clean load.
    with pytest.raises(YAML_LOAD_ERRORS):
        loader.load("%YAML 1.1\n---\nkey: [unclosed\n")

    assert loader.load("key: on\n") == {"key": "on"}


def test_load_reuses_the_underlying_loader_until_a_directive_touches_it():
    # The reset works by discarding the underlying loader, because clearing `YAML.version`
    # alone does not rebuild the versioned resolver on every ruamel release the project
    # declares. That has to stay the rare path: an ordinary document must not pay for a
    # parser construction, which is the whole reason these boundaries keep one instance.
    loader = SafeYamlLoader()
    original = loader._yaml

    loader.load("key: on\n")
    loader.load("other: 1\n")
    assert loader._yaml is original

    loader.load("%YAML 1.1\n---\nkey: on\n")
    directive_recorded = original.version is not None  # clib never records the directive.
    loader.load("key: on\n")
    assert (loader._yaml is not original) is directive_recorded


def test_separate_loaders_do_not_share_version_state():
    # Each boundary builds its own instance precisely so one module's directive cannot steer
    # another module's parse. Asserted the same one-sided way, and for the same reason, as
    # test_load_resets_the_yaml_version_between_documents above.
    first_loader = SafeYamlLoader()
    second_loader = SafeYamlLoader()

    first_loader.load("%YAML 1.1\n---\nkey: on\n")

    assert second_loader.load("key: on\n") == {"key": "on"}


def test_load_errors_family_covers_the_scanner_and_parser():
    with pytest.raises(YAML_LOAD_ERRORS):
        SafeYamlLoader().load("key: [unclosed\n")


def test_load_errors_family_covers_a_rejected_tagged_scalar():
    # `!!int oops` fails inside the constructor, which raises the builtin its type rejected
    # the value with rather than a YAMLError. The tuple exists to catch both.
    with pytest.raises(YAML_LOAD_ERRORS) as exc:
        SafeYamlLoader().load("count: !!int oops\n")

    assert not isinstance(exc.value, YAMLError)


def test_load_errors_family_covers_a_duplicate_key_in_an_ordered_map():
    # ruamel's safe `construct_yaml_omap` enforces `!!omap` key uniqueness with a bare
    # `assert`, so a duplicate key leaves the loader as an AssertionError rather than a
    # YAMLError and used to escape every caller's handler as an uncaught traceback.
    with pytest.raises(YAML_LOAD_ERRORS) as exc:
        SafeYamlLoader().load("!!omap\n- a: 1\n- a: 2\n")

    assert not isinstance(exc.value, YAMLError)


@pytest.mark.parametrize("error_type", [YAMLError, ValueError, KeyError, TypeError, AssertionError])
def test_load_errors_family_is_the_documented_membership(error_type):
    assert error_type in YAML_LOAD_ERRORS
