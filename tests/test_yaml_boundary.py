"""Tests for the shared ruamel safe-load boundary."""

import pytest
from ruamel.yaml.error import ReusedAnchorWarning, YAMLError

from doc_lattice.yaml_boundary import YAML_LOAD_ERRORS, SafeYamlLoader

# Everything below the parser-choice group is a claim about the boundary's own mechanics, not
# about either parser, so each one is asserted on both implementations this loader can be built
# on. Which parser the "platform-default" half actually runs is decided by whether the optional
# `ruamel.yaml.clib` accelerator is installed, and the `yaml-compatibility` CI leg runs both
# answers to that.
BOTH_PARSERS = pytest.mark.parametrize("parser", ["pure", "platform-default"])


def test_a_loader_records_the_parser_implementation_it_was_asked_for():
    # Parser choice is explicit at every construction rather than left to whether the optional
    # `ruamel.yaml.clib` accelerator happens to be installed. `frontmatter_parser` asks for the
    # pure one so a tracked-document verdict is the same in both environments (AD-33); `config`
    # keeps the platform default, whose semantics that pin deliberately leaves alone.
    assert SafeYamlLoader(parser="pure").parser == "pure"
    assert SafeYamlLoader(parser="platform-default").parser == "platform-default"
    # Only the pure arm discriminates on the underlying loader. A plain `YAML(typ="safe")`
    # already reports `pure is False`, so asserting that for the platform default would pass
    # whether or not the choice reached ruamel at all.
    assert SafeYamlLoader(parser="pure")._yaml.pure is True


def test_a_loader_reports_the_parser_implementation_actually_in_hand():
    # `parser` is the request and reads "platform-default" in both environments, so it cannot
    # tell a `ConfigError` raised under the accelerator from one raised without it. `config`
    # names this instead. Only the pure arm is asserted unconditionally: what the platform
    # default resolves to is exactly what the `yaml-compatibility` legs vary.
    assert SafeYamlLoader(parser="pure").running_pure is True


def test_the_parser_implementation_has_no_default():
    # An unstated choice is the defect this argument exists to remove: it resolved to whichever
    # parser the surrounding environment happened to supply. A caller that forgets to choose
    # must fail at construction rather than inherit that.
    with pytest.raises(TypeError):
        SafeYamlLoader()  # ty: ignore[missing-argument]


def test_the_parser_choice_survives_a_directive_reset():
    # The reset works by discarding the underlying loader, so the replacement is a second
    # construction site. A choice applied only at `__init__` would silently lapse for every
    # document read after a `%YAML` directive.
    #
    # Asserted on the pure arm alone, and unconditionally. Only the pure parser records a
    # directive in `YAML.version`, so it is the only arm where a reset fires at all and the only
    # one where this property has content; guarding the assertion on "if a reset happened" would
    # let the test pass with nothing asserted on either arm the day `load` stopped rebuilding.
    loader = SafeYamlLoader(parser="pure")
    original = loader._yaml

    loader.load("%YAML 1.1\n---\nkey: on\n")
    loader.load("key: on\n")

    assert loader._yaml is not original
    assert loader._yaml.pure is True
    assert loader.parser == "pure"


def test_a_pure_loader_accepts_an_anchor_name_defined_twice():
    # The pure parser warns and rebinds the name; the C composer refuses it outright. This is
    # the difference the pure pin exists to remove from the tracked-document verdict, so it is
    # asserted here on both `yaml-compatibility` legs rather than probed at import time.
    #
    # The warning escapes here because this is the raw boundary. `parse_meta` captures it and
    # re-reports it as a project diagnostic naming the file, so that a warm cache replays it
    # (AD-29); this loader has no file to name and no cache to consult.
    with pytest.warns(ReusedAnchorWarning):
        loaded = SafeYamlLoader(parser="pure").load(
            "first: &name 1\nsecond: &name 2\nthird: *name\n"
        )

    assert loaded == {"first": 1, "second": 2, "third": 2}


@BOTH_PARSERS
def test_load_returns_the_constructed_document(parser):
    assert SafeYamlLoader(parser=parser).load("a: 1\nb: [x, y]\n") == {"a": 1, "b": ["x", "y"]}


@BOTH_PARSERS
def test_load_returns_none_for_an_empty_document(parser):
    # The None is deliberately not normalized here: config owns the `None -> {}` fallback,
    # and frontmatter treats a non-dict as "not a lattice node", so the two callers want
    # different things from an empty file.
    assert SafeYamlLoader(parser=parser).load("") is None


@BOTH_PARSERS
def test_load_resets_the_yaml_version_between_documents(parser):
    loader = SafeYamlLoader(parser=parser)

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
def test_load_resets_the_yaml_version_after_a_failed_parse(parser):
    loader = SafeYamlLoader(parser=parser)

    # A directive can update the parser's version even when the document it heads fails to
    # parse, so the reset has to survive the exception rather than run after a clean load.
    with pytest.raises(YAML_LOAD_ERRORS):
        loader.load("%YAML 1.1\n---\nkey: [unclosed\n")

    assert loader.load("key: on\n") == {"key": "on"}


@BOTH_PARSERS
def test_load_reuses_the_underlying_loader_until_a_directive_touches_it(parser):
    # The reset works by discarding the underlying loader, because clearing `YAML.version`
    # alone does not rebuild the versioned resolver on every ruamel release the project
    # declares. That has to stay the rare path: an ordinary document must not pay for a
    # parser construction, which is the whole reason these boundaries keep one instance.
    loader = SafeYamlLoader(parser=parser)
    original = loader._yaml

    loader.load("key: on\n")
    loader.load("other: 1\n")
    assert loader._yaml is original

    loader.load("%YAML 1.1\n---\nkey: on\n")
    directive_recorded = original.version is not None  # clib never records the directive.
    loader.load("key: on\n")
    assert (loader._yaml is not original) is directive_recorded


@BOTH_PARSERS
def test_separate_loaders_do_not_share_version_state(parser):
    # Each boundary builds its own instance precisely so one module's directive cannot steer
    # another module's parse. Asserted the same one-sided way, and for the same reason, as
    # test_load_resets_the_yaml_version_between_documents above.
    first_loader = SafeYamlLoader(parser=parser)
    second_loader = SafeYamlLoader(parser=parser)

    first_loader.load("%YAML 1.1\n---\nkey: on\n")

    assert second_loader.load("key: on\n") == {"key": "on"}


@BOTH_PARSERS
def test_load_errors_family_covers_the_scanner_and_parser(parser):
    with pytest.raises(YAML_LOAD_ERRORS):
        SafeYamlLoader(parser=parser).load("key: [unclosed\n")


@BOTH_PARSERS
def test_load_errors_family_covers_a_rejected_tagged_scalar(parser):
    # `!!int oops` fails inside the constructor, which raises the builtin its type rejected
    # the value with rather than a YAMLError. The tuple exists to catch both.
    with pytest.raises(YAML_LOAD_ERRORS) as exc:
        SafeYamlLoader(parser=parser).load("count: !!int oops\n")

    assert not isinstance(exc.value, YAMLError)


@BOTH_PARSERS
def test_load_errors_family_covers_a_duplicate_key_in_an_ordered_map(parser):
    # ruamel's safe `construct_yaml_omap` enforces `!!omap` key uniqueness with a bare
    # `assert`, so a duplicate key leaves the loader as an AssertionError rather than a
    # YAMLError and used to escape every caller's handler as an uncaught traceback.
    with pytest.raises(YAML_LOAD_ERRORS) as exc:
        SafeYamlLoader(parser=parser).load("!!omap\n- a: 1\n- a: 2\n")

    assert not isinstance(exc.value, YAMLError)


@pytest.mark.parametrize("error_type", [YAMLError, ValueError, KeyError, TypeError, AssertionError])
def test_load_errors_family_is_the_documented_membership(error_type):
    assert error_type in YAML_LOAD_ERRORS
