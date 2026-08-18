"""Tests for the shared ruamel safe-load boundary."""

import pytest
from ruamel.yaml.error import ReusedAnchorWarning, YAMLError

from doc_lattice.yaml_boundary import YAML_LOAD_ERRORS, SafeYamlLoader

# Everything below the parser-choice group is a claim about the boundary's own mechanics, not
# about either parser, so each one is asserted on both implementations this loader can be built
# on. Which of them the `pure=False` half actually runs is decided by whether the optional
# `ruamel.yaml.clib` accelerator is installed, and the `yaml-compatibility` CI leg runs both
# answers to that.
BOTH_PARSERS = pytest.mark.parametrize("pure", [True, False])


def test_a_loader_records_the_parser_implementation_it_was_asked_for():
    # Parser choice is explicit at every construction rather than left to whether the optional
    # `ruamel.yaml.clib` accelerator happens to be installed. `frontmatter_parser` asks for the
    # pure one so a tracked-document verdict is the same in both environments (AD-31); `config`
    # keeps the default, whose semantics that pin deliberately leaves alone.
    assert SafeYamlLoader(pure=True)._yaml.pure is True
    assert SafeYamlLoader(pure=False)._yaml.pure is False


def test_the_parser_implementation_has_no_default():
    # An unstated choice is the defect this argument exists to remove: it resolved to whichever
    # parser the surrounding environment happened to supply. A caller that forgets to choose
    # must fail at construction rather than inherit that.
    with pytest.raises(TypeError):
        SafeYamlLoader()  # ty: ignore[missing-argument]


@BOTH_PARSERS
def test_the_parser_choice_survives_a_directive_reset(pure):
    # The reset works by discarding the underlying loader, so the replacement is a second
    # construction site. A choice applied only at `__init__` would silently lapse for every
    # document read after a `%YAML` directive.
    loader = SafeYamlLoader(pure=pure)
    original = loader._yaml

    loader.load("%YAML 1.1\n---\nkey: on\n")
    loader.load("key: on\n")

    if loader._yaml is not original:
        assert loader._yaml.pure is pure


def test_a_pure_loader_accepts_an_anchor_name_defined_twice():
    # The pure parser warns and rebinds the name; the C composer refuses it outright. This is
    # the difference the pure pin exists to remove from the tracked-document verdict, so it is
    # asserted here on both `yaml-compatibility` legs rather than probed at import time.
    with pytest.warns(ReusedAnchorWarning):
        loaded = SafeYamlLoader(pure=True).load("first: &name 1\nsecond: &name 2\nthird: *name\n")

    assert loaded == {"first": 1, "second": 2, "third": 2}


@BOTH_PARSERS
def test_load_returns_the_constructed_document(pure):
    assert SafeYamlLoader(pure=pure).load("a: 1\nb: [x, y]\n") == {"a": 1, "b": ["x", "y"]}


@BOTH_PARSERS
def test_load_returns_none_for_an_empty_document(pure):
    # The None is deliberately not normalized here: config owns the `None -> {}` fallback,
    # and frontmatter treats a non-dict as "not a lattice node", so the two callers want
    # different things from an empty file.
    assert SafeYamlLoader(pure=pure).load("") is None


@BOTH_PARSERS
def test_load_resets_the_yaml_version_between_documents(pure):
    loader = SafeYamlLoader(pure=pure)

    # YAML 1.1 resolves an unquoted `on` to True, 1.2 keeps it the string "on", so a version
    # that leaked forward from the directive would turn the second load's value into True.
    # Only the second load is asserted. Whether the first document itself resolves under 1.1
    # depends on which parser implementation the loader was built on: the optional
    # `ruamel.yaml.clib` accelerator skips the directive and resolver state entirely (AD-26),
    # and the `yaml-compatibility` CI leg runs the suite with it present. The no-leak property
    # holds under both, which is the property this helper is responsible for.
    loader.load("%YAML 1.1\n---\nkey: on\n")

    assert loader.load("key: on\n") == {"key": "on"}


@BOTH_PARSERS
def test_load_resets_the_yaml_version_after_a_failed_parse(pure):
    loader = SafeYamlLoader(pure=pure)

    # A directive can update the parser's version even when the document it heads fails to
    # parse, so the reset has to survive the exception rather than run after a clean load.
    with pytest.raises(YAML_LOAD_ERRORS):
        loader.load("%YAML 1.1\n---\nkey: [unclosed\n")

    assert loader.load("key: on\n") == {"key": "on"}


@BOTH_PARSERS
def test_load_reuses_the_underlying_loader_until_a_directive_touches_it(pure):
    # The reset works by discarding the underlying loader, because clearing `YAML.version`
    # alone does not rebuild the versioned resolver on every ruamel release the project
    # declares. That has to stay the rare path: an ordinary document must not pay for a
    # parser construction, which is the whole reason these boundaries keep one instance.
    loader = SafeYamlLoader(pure=pure)
    original = loader._yaml

    loader.load("key: on\n")
    loader.load("other: 1\n")
    assert loader._yaml is original

    loader.load("%YAML 1.1\n---\nkey: on\n")
    directive_recorded = original.version is not None  # clib never records the directive.
    loader.load("key: on\n")
    assert (loader._yaml is not original) is directive_recorded


@BOTH_PARSERS
def test_separate_loaders_do_not_share_version_state(pure):
    # Each boundary builds its own instance precisely so one module's directive cannot steer
    # another module's parse. Asserted the same one-sided way, and for the same reason, as
    # test_load_resets_the_yaml_version_between_documents above.
    first_loader = SafeYamlLoader(pure=pure)
    second_loader = SafeYamlLoader(pure=pure)

    first_loader.load("%YAML 1.1\n---\nkey: on\n")

    assert second_loader.load("key: on\n") == {"key": "on"}


@BOTH_PARSERS
def test_load_errors_family_covers_the_scanner_and_parser(pure):
    with pytest.raises(YAML_LOAD_ERRORS):
        SafeYamlLoader(pure=pure).load("key: [unclosed\n")


@BOTH_PARSERS
def test_load_errors_family_covers_a_rejected_tagged_scalar(pure):
    # `!!int oops` fails inside the constructor, which raises the builtin its type rejected
    # the value with rather than a YAMLError. The tuple exists to catch both.
    with pytest.raises(YAML_LOAD_ERRORS) as exc:
        SafeYamlLoader(pure=pure).load("count: !!int oops\n")

    assert not isinstance(exc.value, YAMLError)


@BOTH_PARSERS
def test_load_errors_family_covers_a_duplicate_key_in_an_ordered_map(pure):
    # ruamel's safe `construct_yaml_omap` enforces `!!omap` key uniqueness with a bare
    # `assert`, so a duplicate key leaves the loader as an AssertionError rather than a
    # YAMLError and used to escape every caller's handler as an uncaught traceback.
    with pytest.raises(YAML_LOAD_ERRORS) as exc:
        SafeYamlLoader(pure=pure).load("!!omap\n- a: 1\n- a: 2\n")

    assert not isinstance(exc.value, YAMLError)


@pytest.mark.parametrize("error_type", [YAMLError, ValueError, KeyError, TypeError, AssertionError])
def test_load_errors_family_is_the_documented_membership(error_type):
    assert error_type in YAML_LOAD_ERRORS
