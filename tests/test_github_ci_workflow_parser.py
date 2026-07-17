"""Tests for the GitHub Actions workflow parser boundary."""

from pathlib import Path

import pytest

import doc_lattice.github_ci.workflow_parser as workflow_parser_module
from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci.model import WorkflowStructureEntry
from doc_lattice.github_ci.workflow_parser import parse_workflow

MAX_UTF8_INPUT_BYTES = 1_048_576
MAX_YAML_NESTING_DEPTH = 100
MAX_EXPANDED_VISITS = 50_000
MAX_COLLECTED_STRING_SCALARS = 10_000
MAX_EXPANDED_SCALAR_CHARS = 1_048_576


def _alias_graph(leaf: str, *, layers: int) -> str:
    lines = ["on: push", f"layer0: &layer0 [{leaf}]"]
    for level in range(1, layers + 1):
        aliases = ", ".join([f"*layer{level - 1}"] * 10)
        lines.append(f"layer{level}: &layer{level} [{aliases}]")
    lines.append("jobs: {}")
    return "\n".join(lines) + "\n"


def _nested_sequence_workflow(levels: int) -> str:
    nested = "[" * levels + "'leaf'" + "]" * levels
    return f"on: push\npayload: {nested}\njobs: {{}}\n"


def _workflow_padded_to_bytes(size: int) -> str:
    base = "on: push\njobs: {}\n"
    remaining = size - len(base.encode())
    assert remaining >= 2
    return base + "#" + ("x" * (remaining - 2)) + "\n"


def _workflow_padded_to_bytes_with_multibyte_text(size: int) -> str:
    base = "on: push\njobs: {}\n#"
    remaining = size - len((base + "\n").encode())
    assert remaining >= 2
    return base + ("é" * (remaining // 2)) + ("x" * (remaining % 2)) + "\n"


def _null_visit_workflow(values: int) -> str:
    items = "\n".join("  -" for _ in range(values))
    return f"payload:\n{items}\njobs: {{}}\n"


def test_parse_workflow_normalizes_a_managed_linear_job():
    workflow = parse_workflow(
        Path(".github/workflows/linear.yml"),
        """\
name: Linear
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
jobs:
  linear:
    if: github.repository == 'acme/widgets'
    environment: production
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - id: audit
        env:
          LINEAR_API_KEY: ${{ secrets.LINEAR_API_KEY }}
        run: uv run doc-lattice audit --linear
""",
    )

    assert [(trigger.name, trigger.shape, trigger.branches) for trigger in workflow.triggers] == [
        ("push", "mapping", ("main",)),
        ("workflow_dispatch", "null", None),
    ]
    assert workflow.permissions == (("contents", "read"),)
    assert len(workflow.jobs) == 1
    job = workflow.jobs[0]
    assert job.job_id == "linear"
    assert job.if_condition == "github.repository == 'acme/widgets'"
    assert job.environment == "production"
    assert job.runs_on == "ubuntu-latest"
    assert job.permissions is None
    assert job.env == ()
    assert len(job.steps) == 2
    assert job.steps[0].uses == "actions/checkout@v4"
    assert job.steps[0].with_values == (("persist-credentials", "false"),)
    assert job.steps[1].step_id == "audit"
    assert job.steps[1].env == (("LINEAR_API_KEY", "${{ secrets.LINEAR_API_KEY }}"),)
    assert job.steps[1].run == "uv run doc-lattice audit --linear"
    assert any(
        scalar.path == ("jobs", "linear", "steps", "1", "run")
        and scalar.value == "uv run doc-lattice audit --linear"
        for scalar in workflow.scalars
    )


def test_parse_workflow_normalizes_effective_shell_fields():
    parsed = parse_workflow(
        Path(".github/workflows/shells.yml"),
        """\
on: pull_request
defaults:
  run:
    shell: workflow-shell {0}
jobs:
  audit:
    runs-on: ubuntu-latest
    defaults:
      run:
        shell: job-shell {0}
    steps:
      - shell: step-shell {0}
        run: safe configuration
""",
    )

    assert parsed.default_shell == "workflow-shell {0}"
    assert parsed.jobs[0].default_shell == "job-shell {0}"
    assert parsed.jobs[0].steps[0].shell == "step-shell {0}"


def test_parse_workflow_emits_deterministic_typed_structure():
    first = parse_workflow(
        Path(".github/workflows/structure.yml"),
        """\
jobs:
  check:
    steps:
      - run: echo ok
    empty: {}
on:
  push:
    paths: [docs/**]
env:
  FLAG: true
  COUNT: 2
  RATIO: 1.5
  EMPTY:
""",
    )
    second = parse_workflow(
        Path(".github/workflows/structure.yml"),
        """\
env:
  RATIO: 1.5
  EMPTY:
  COUNT: 2
  FLAG: true
on:
  push:
    paths:
      - docs/**
jobs:
  check:
    empty: {}
    steps:
      - run: echo ok
""",
    )

    assert first.structure == second.structure
    assert WorkflowStructureEntry((), "mapping", None) in first.structure
    assert WorkflowStructureEntry(("jobs", "check", "empty"), "mapping", None) in first.structure
    assert WorkflowStructureEntry(("on", "push", "paths"), "sequence", None) in first.structure
    assert (
        WorkflowStructureEntry(("on", "push", "paths", "0"), "string", "docs/**") in first.structure
    )
    assert WorkflowStructureEntry(("env", "FLAG"), "boolean", "true") in first.structure
    assert WorkflowStructureEntry(("env", "COUNT"), "integer", "2") in first.structure
    assert WorkflowStructureEntry(("env", "RATIO"), "float", "1.5") in first.structure
    assert WorkflowStructureEntry(("env", "EMPTY"), "null", None) in first.structure


def test_parse_workflow_keeps_implicit_timestamps_as_strings():
    parsed = parse_workflow(
        Path(".github/workflows/timestamps.yml"),
        """\
env:
  RELEASE_DATE: 2026-07-17
  RELEASE_AT: 2026-07-17T14:30:00Z
jobs: {}
""",
    )

    values = {scalar.path: scalar.value for scalar in parsed.scalars}
    assert values[("env", "RELEASE_DATE")] == "2026-07-17"
    assert values[("env", "RELEASE_AT")] == "2026-07-17T14:30:00Z"


def test_parse_workflow_rejects_explicit_timestamp_tag():
    with pytest.raises(ConfigError, match="unsupported YAML scalar"):
        parse_workflow(
            Path(".github/workflows/tagged-timestamp.yml"),
            "payload: !!timestamp 2026-07-17\njobs: {}\n",
        )


def test_parse_workflow_structure_distinguishes_missing_empty_and_sequence_order():
    missing = parse_workflow(Path("base.yml"), "on: push\njobs:\n  check:\n    steps: []\n")
    empty = parse_workflow(
        Path("base.yml"),
        "on: push\njobs:\n  check:\n    env: {}\n    steps: []\n",
    )
    ordered = parse_workflow(
        Path("base.yml"),
        "on: push\njobs:\n  check:\n    steps:\n      - run: one\n      - run: two\n",
    )
    reversed_order = parse_workflow(
        Path("base.yml"),
        "on: push\njobs:\n  check:\n    steps:\n      - run: two\n      - run: one\n",
    )

    assert missing.structure != empty.structure
    assert ordered.structure != reversed_order.structure


@pytest.mark.parametrize(
    ("event_yaml", "expected"),
    [
        ("on: pull_request", [("pull_request", "null", None)]),
        (
            "on: [push, pull_request]",
            [("push", "null", None), ("pull_request", "null", None)],
        ),
        (
            "on: {pull_request_target: null}",
            [("pull_request_target", "null", None)],
        ),
        (
            "on: {schedule: [{cron: '17 3 * * *'}]}",
            [("schedule", "sequence", None)],
        ),
    ],
)
def test_parse_workflow_accepts_event_shorthands(event_yaml, expected):
    parsed = parse_workflow(
        Path(".github/workflows/events.yml"),
        f"{event_yaml}\njobs: {{}}\n",
    )

    assert [
        (trigger.name, trigger.shape, trigger.branches) for trigger in parsed.triggers
    ] == expected


def test_parse_workflow_accepts_unrelated_matrix_reusable_and_schedule_shapes():
    parsed = parse_workflow(
        Path(".github/workflows/unrelated.yml"),
        """\
name: Unrelated
on:
  schedule:
    - cron: "17 3 * * *"
  pull_request:
    types: [opened, synchronize]
jobs:
  matrix:
    strategy:
      matrix:
        python: ["3.13", "3.14"]
        os: [ubuntu-latest, windows-latest]
        include:
          - os: ubuntu-latest
            experimental: true
    environment:
      name: production
      url: https://example.test/deploy
    runs-on: [self-hosted, "${{ matrix.os }}"]
    steps:
      - name: Test
        run: uv run pytest
  reusable:
    uses: acme/automation/.github/workflows/reusable.yml@main
    with:
      dry-run: false
    secrets: inherit
""",
    )

    assert [(trigger.name, trigger.shape) for trigger in parsed.triggers] == [
        ("schedule", "sequence"),
        ("pull_request", "mapping"),
    ]
    assert [job.job_id for job in parsed.jobs] == ["matrix", "reusable"]
    assert parsed.jobs[0].environment is None
    assert parsed.jobs[0].runs_on is None
    assert parsed.jobs[1].steps == ()
    assert any(
        scalar.path == ("jobs", "matrix", "strategy", "matrix", "python", "0")
        and scalar.value == "3.13"
        for scalar in parsed.scalars
    )
    assert any(
        scalar.path == ("jobs", "reusable", "uses")
        and scalar.value == "acme/automation/.github/workflows/reusable.yml@main"
        for scalar in parsed.scalars
    )


def test_parse_workflow_normalizes_permissions_env_and_with_scalars():
    parsed = parse_workflow(
        Path(".github/workflows/scalars.yml"),
        """\
on: push
permissions: read-all
jobs:
  audit:
    permissions:
      actions: write
      contents: read
    env:
      BOOL: true
      COUNT: 3
      FRACTION: 1.5
      TEXT: unchanged
    steps:
      - id: 17
        name: false
        uses: 4
        run: true
        env:
          ENABLED: false
        with:
          attempts: 2
          enabled: true
          mode: strict
""",
    )

    assert parsed.permissions == "read-all"
    job = parsed.jobs[0]
    assert job.permissions == (("actions", "write"), ("contents", "read"))
    assert job.env == (
        ("BOOL", "true"),
        ("COUNT", "3"),
        ("FRACTION", "1.5"),
        ("TEXT", "unchanged"),
    )
    step = job.steps[0]
    assert step.step_id is None
    assert step.name is None
    assert step.uses is None
    assert step.run is None
    assert step.env == (("ENABLED", "false"),)
    assert step.with_values == (
        ("attempts", "2"),
        ("enabled", "true"),
        ("mode", "strict"),
    )


def test_parse_workflow_preserves_only_string_optional_job_fields():
    parsed = parse_workflow(
        Path(".github/workflows/optional.yml"),
        """\
on: push
jobs:
  audit:
    if: false
    environment: 7
    runs-on: true
""",
    )

    job = parsed.jobs[0]
    assert job.if_condition is None
    assert job.environment is None
    assert job.runs_on is None


@pytest.mark.parametrize(
    "text",
    [
        "on: [push\njobs: {}\n",
        "on: push\non: pull_request\njobs: {}\n",
        "- on\n- push\n",
        "on: {push: 3}\njobs: {}\n",
        "on: push\njobs: []\n",
        "on: push\n",
    ],
    ids=[
        "malformed-sequence",
        "duplicate-top-level-on",
        "top-level-sequence",
        "unsupported-event-scalar",
        "jobs-sequence",
        "missing-jobs",
    ],
)
def test_parse_workflow_rejects_unreliable_top_level_shapes(text):
    path = Path(".github/workflows/broken.yml")

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, text)

    assert exc.value.code == "CONFIG_ERROR"
    assert str(path) in str(exc.value)


@pytest.mark.parametrize(
    "text",
    [
        "on: push\njobs:\n  audit:\n    env: {}\n    env: {}\n",
        (
            "on: push\njobs:\n  audit:\n    steps:\n"
            "      - env:\n          TOKEN: one\n          TOKEN: two\n"
        ),
    ],
    ids=["duplicate-job-key", "duplicate-step-env-key"],
)
def test_parse_workflow_rejects_duplicate_nested_keys(text):
    with pytest.raises(ConfigError) as exc:
        parse_workflow(Path(".github/workflows/duplicate.yml"), text)

    assert ".github/workflows/duplicate.yml" in str(exc.value)


@pytest.mark.parametrize(
    "text",
    [
        (
            "on: push\ndefaults: &defaults\n  runs-on: ubuntu-latest\njobs:\n  audit:\n"
            "    <<: *defaults\n    steps: []\n"
        ),
        (
            "on: push\nleft: &left\n  runs-on: ubuntu-latest\n"
            "right: &right\n  runs-on: windows-latest\njobs:\n  audit:\n"
            "    <<: [*left, *right]\n    steps: []\n"
        ),
    ],
    ids=["single-source", "overlapping-multiple-sources"],
)
def test_parse_workflow_rejects_yaml_merge_keys(text):
    path = Path(".github/workflows/merge.yml")

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, text)

    assert str(path) in str(exc.value)
    assert "merge key" in str(exc.value)


def test_parse_workflow_accepts_ordinary_anchors_without_merge_keys(capsys):
    parsed = parse_workflow(
        Path(".github/workflows/anchor.yml"),
        """\
on: push
command: &audit_command uv run doc-lattice audit
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: *audit_command
""",
    )

    assert parsed.jobs[0].steps[0].run == "uv run doc-lattice audit"
    assert capsys.readouterr().err == ""


def test_parse_workflow_rejects_duplicate_anchors_without_warning_leak(capsys):
    path = Path(".github/workflows/duplicate-anchor.yml")
    text = """\
on: push
first: &SENSITIVE_ANCHOR FIRST_SECRET
second: &SENSITIVE_ANCHOR SECOND_SECRET
jobs: {}
"""

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, text)

    message = str(exc.value)
    captured = capsys.readouterr()
    assert str(path) in message
    assert "duplicate YAML anchor" in message
    assert "SENSITIVE_ANCHOR" not in message
    assert "FIRST_SECRET" not in message
    assert "SECOND_SECRET" not in message
    assert captured.err == ""


def test_parse_workflow_expands_shared_aliases_at_each_path_in_order():
    parsed = parse_workflow(
        Path(".github/workflows/shared-alias.yml"),
        """\
shared: &shared [first, second]
copies: [*shared, *shared]
jobs: {}
""",
    )

    assert [
        (scalar.path, scalar.value) for scalar in parsed.scalars if scalar.path[:1] == ("copies",)
    ] == [
        (("copies", "0", "0"), "first"),
        (("copies", "0", "1"), "second"),
        (("copies", "1", "0"), "first"),
        (("copies", "1", "1"), "second"),
    ]


def test_parse_workflow_rejects_recursive_aliases():
    with pytest.raises(ConfigError) as exc:
        parse_workflow(
            Path(".github/workflows/recursive.yml"),
            "recursive: &recursive [*recursive]\njobs: {}\n",
        )

    assert "recursive YAML aliases" in str(exc.value)


def test_parse_workflow_rejects_alias_expansion_over_scalar_budget():
    path = Path(".github/workflows/aliases.yml")

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, _alias_graph("LEAK_ME", layers=4))

    assert str(path) in str(exc.value)
    assert "resource limit" in str(exc.value)
    assert "LEAK_ME" not in str(exc.value)


def test_parse_workflow_rejects_alias_expansion_over_scalar_volume_budget():
    path = Path(".github/workflows/aliases.yml")
    command = "x" * (MAX_EXPANDED_SCALAR_CHARS // 2)
    text = f"""\
on: pull_request
command: &command {command}
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - shell: bash
        run: *command
"""
    assert len(text.encode("utf-8")) < MAX_UTF8_INPUT_BYTES

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, text)

    assert str(path) in str(exc.value)
    assert "resource limit" in str(exc.value)


def test_parse_workflow_rejects_alias_expansion_over_visit_budget():
    path = Path(".github/workflows/aliases.yml")

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, _alias_graph("null", layers=5))

    assert str(path) in str(exc.value)
    assert "resource limit" in str(exc.value)
    assert str(MAX_EXPANDED_VISITS) not in str(exc.value)


def test_parse_workflow_accepts_exact_expanded_visit_budget():
    # With two root entries, N null list items consume exactly 2*N + 8 syntax/value visits.
    null_values = (MAX_EXPANDED_VISITS - 8) // 2
    parsed = parse_workflow(
        Path(".github/workflows/visit-boundary.yml"),
        _null_visit_workflow(null_values),
    )

    assert parsed.jobs == ()


def test_parse_workflow_rejects_first_visit_over_budget():
    null_values = (MAX_EXPANDED_VISITS - 8) // 2 + 1

    with pytest.raises(ConfigError) as exc:
        parse_workflow(
            Path(".github/workflows/visit-over.yml"),
            _null_visit_workflow(null_values),
        )

    assert "resource limit" in str(exc.value)


def test_parse_workflow_accepts_exact_scalar_budget():
    values = "\n".join("  - value" for _ in range(MAX_COLLECTED_STRING_SCALARS - 1))
    parsed = parse_workflow(
        Path(".github/workflows/scalar-boundary.yml"),
        f"on: push\nvalues:\n{values}\njobs: {{}}\n",
    )

    assert len(parsed.scalars) == MAX_COLLECTED_STRING_SCALARS


def test_parse_workflow_accepts_exact_input_byte_budget():
    parsed = parse_workflow(
        Path(".github/workflows/input-boundary.yml"),
        _workflow_padded_to_bytes(MAX_UTF8_INPUT_BYTES),
    )

    assert parsed.jobs == ()


def test_parse_workflow_accepts_exact_multibyte_input_byte_budget():
    text = _workflow_padded_to_bytes_with_multibyte_text(MAX_UTF8_INPUT_BYTES)
    assert len(text) < MAX_UTF8_INPUT_BYTES
    assert len(text.encode("utf-8")) == MAX_UTF8_INPUT_BYTES

    parsed = parse_workflow(
        Path(".github/workflows/multibyte-input-boundary.yml"),
        text,
    )

    assert parsed.jobs == ()


def test_parse_workflow_rejects_input_over_byte_budget_before_parsing():
    path = Path(".github/workflows/oversize.yml")

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, _workflow_padded_to_bytes(MAX_UTF8_INPUT_BYTES + 1))

    assert str(path) in str(exc.value)
    assert "resource limit" in str(exc.value)


def test_parse_workflow_stops_byte_count_before_invalid_oversize_tail():
    path = Path(".github/workflows/oversize-tail.yml")
    text = ("x" * (MAX_UTF8_INPUT_BYTES + 8_192)) + "\ud800"

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, text)

    assert str(path) in str(exc.value)
    assert "resource limit" in str(exc.value)
    assert "malformed YAML" not in str(exc.value)


def test_traversal_budget_reserves_pending_visits_before_queueing():
    budget = workflow_parser_module._TraversalBudget(Path(".github/workflows/reservation.yml"))
    budget.reserve_visits(40_000, depth=1)

    with pytest.raises(ConfigError) as exc:
        budget.reserve_visits(10_001, depth=2)

    assert "resource limit" in str(exc.value)


def test_parse_workflow_accepts_exact_nesting_depth_budget():
    parsed = parse_workflow(
        Path(".github/workflows/depth-boundary.yml"),
        _nested_sequence_workflow(MAX_YAML_NESTING_DEPTH - 2),
    )

    assert parsed.jobs == ()


def test_parse_workflow_rejects_nesting_over_depth_budget():
    path = Path(".github/workflows/deep.yml")

    with pytest.raises(ConfigError) as exc:
        parse_workflow(
            path,
            _nested_sequence_workflow(MAX_YAML_NESTING_DEPTH - 1),
        )

    assert str(path) in str(exc.value)
    assert "resource limit" in str(exc.value)


def test_parse_workflow_wraps_oversized_numeric_scalar_conversion():
    path = Path(".github/workflows/number.yml")
    digits = "9" * 5_000

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, f"on: push\npayload: {digits}\njobs: {{}}\n")

    message = str(exc.value)
    assert str(path) in message
    assert "malformed YAML" in message
    assert "999999999999" not in message
    assert "ValueError" not in message


def test_parse_workflow_wraps_invalid_unicode_encoding():
    path = Path(".github/workflows/unicode.yml")

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, "on: push\npayload: '\ud800'\njobs: {}\n")

    assert str(path) in str(exc.value)
    assert "malformed YAML" in str(exc.value)


def test_parse_workflow_uses_yaml_1_2_semantics_by_default():
    parsed = parse_workflow(
        Path(".github/workflows/yaml-default.yml"),
        """\
on: push
jobs:
  audit:
    env:
      NO_VALUE: no
      OFF_VALUE: off
      ON_VALUE: on
      YES_VALUE: yes
      FALSE_VALUE: false
      TRUE_VALUE: true
""",
    )

    assert parsed.triggers[0].name == "push"
    assert parsed.jobs[0].env == (
        ("FALSE_VALUE", "false"),
        ("NO_VALUE", "no"),
        ("OFF_VALUE", "off"),
        ("ON_VALUE", "on"),
        ("TRUE_VALUE", "true"),
        ("YES_VALUE", "yes"),
    )


def test_parse_workflow_accepts_explicit_yaml_1_2():
    parsed = parse_workflow(
        Path(".github/workflows/yaml-1.2.yml"),
        "%YAML 1.2\n---\non: push\njobs: {}\n",
    )

    assert parsed.triggers[0].name == "push"


def test_parse_workflow_rejects_explicit_yaml_1_1():
    path = Path(".github/workflows/yaml-1.1.yml")

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, "%YAML 1.1\n---\non: push\njobs: {}\n")

    assert str(path) in str(exc.value)
    assert "unsupported YAML version" in str(exc.value)


@pytest.mark.parametrize("directive", ["%YAML 1.0", "%YAML 1.3", "%YAML 1.4"])
def test_parse_workflow_wraps_asserting_unsupported_yaml_versions(directive):
    path = Path(".github/workflows/yaml-unsupported.yml")

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, f"{directive}\n---\non: push\njobs: {{}}\n")

    message = str(exc.value)
    assert str(path) in message
    assert "unsupported YAML version" in message
    assert "AssertionError" not in message
    assert directive not in message


@pytest.mark.parametrize(
    "text",
    [
        "7: value\non: push\njobs: {}\n",
        "on: push\njobs:\n  7: {}\n",
        "on: push\njobs:\n  audit:\n    strategy:\n      matrix:\n        3.13: ubuntu-latest\n",
        "on: push\njobs:\n  audit:\n    steps:\n      - env:\n          1: value\n",
    ],
    ids=["top-level", "job-id", "unrelated-nested-key", "env-key"],
)
def test_parse_workflow_rejects_non_string_mapping_keys_anywhere(text):
    with pytest.raises(ConfigError):
        parse_workflow(Path(".github/workflows/non-string-key.yml"), text)


@pytest.mark.parametrize(
    "branches_yaml",
    [
        "3",
        "true",
        "{}",
        "[main, 7]",
        "[[main]]",
        "null",
    ],
)
def test_parse_workflow_rejects_invalid_branch_filters(branches_yaml):
    text = f"on:\n  push:\n    branches: {branches_yaml}\njobs: {{}}\n"

    with pytest.raises(ConfigError) as exc:
        parse_workflow(Path(".github/workflows/branches.yml"), text)

    assert "branches" in str(exc.value)


@pytest.mark.parametrize(
    "text",
    [
        "on: push\npermissions: [read-all]\njobs: {}\n",
        "on: push\npermissions:\n  contents: true\njobs: {}\n",
        "on: push\njobs:\n  audit:\n    permissions: {}\n    env: [TOKEN]\n",
        "on: push\njobs:\n  audit:\n    env:\n      TOKEN: [nested]\n",
        "on: push\njobs:\n  audit:\n    permissions:\n      contents: [read]\n",
        "on: push\njobs:\n  audit:\n    steps: {}\n",
        "on: push\njobs:\n  audit:\n    steps:\n      - run\n",
        "on: push\njobs:\n  audit:\n    steps:\n      - env: TOKEN\n",
        "on: push\njobs:\n  audit:\n    steps:\n      - with: [value]\n",
        "on: push\njobs:\n  audit:\n    steps:\n      - env:\n          TOKEN: null\n",
        "on: push\njobs:\n  audit:\n    steps:\n      - with:\n          option: [nested]\n",
    ],
    ids=[
        "top-permissions-sequence",
        "top-permissions-non-string-value",
        "job-env-sequence",
        "job-env-container-value",
        "job-permissions-container-value",
        "steps-mapping",
        "step-scalar",
        "step-env-scalar",
        "step-with-sequence",
        "step-env-null-value",
        "step-with-container-value",
    ],
)
def test_parse_workflow_rejects_invalid_audited_containers(text):
    with pytest.raises(ConfigError) as exc:
        parse_workflow(Path(".github/workflows/container.yml"), text)

    assert ".github/workflows/container.yml" in str(exc.value)


@pytest.mark.parametrize(
    "text",
    [
        "on: [push, 7]\njobs: {}\n",
        "on:\n  push: null\n  7: null\njobs: {}\n",
        "on:\n  push:\n    branches: main\n    paths:\n      - src/**\njobs:\n  audit: scalar\n",
        "on: push\njobs:\n  audit:\n    if: [always]\n",
        "on: push\njobs:\n  audit:\n    steps:\n      - name: [Audit]\n",
    ],
    ids=[
        "event-list-non-string",
        "event-mapping-non-string-key",
        "job-scalar",
        "job-if-container",
        "step-name-container",
    ],
)
def test_parse_workflow_rejects_other_unreliable_audited_shapes(text):
    with pytest.raises(ConfigError):
        parse_workflow(Path(".github/workflows/unreliable.yml"), text)


def test_parse_workflow_error_uses_display_path_without_leaking_checkout_path(
    tmp_path, monkeypatch
):
    checkout = tmp_path / "secret-checkout-name"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    display_path = Path(".github/workflows/broken.yml")
    text = "on: [push\njobs: {}\n"

    with pytest.raises(ConfigError) as exc:
        parse_workflow(display_path, text)

    assert str(display_path) in str(exc.value)
    assert str(checkout) not in str(exc.value)


def test_parse_workflow_escapes_control_characters_in_display_path():
    path = Path(".github/workflows/bad\nname\x1b.yml")

    with pytest.raises(ConfigError) as exc:
        parse_workflow(path, "on: [push\njobs: {}\n")

    message = str(exc.value)
    assert "\n" not in message
    assert "\x1b" not in message
    assert r"\n" in message
    assert r"\u001b" in message


def test_parse_workflow_renders_yaml_path_components_unambiguously():
    with pytest.raises(ConfigError) as exc:
        parse_workflow(
            Path(".github/workflows/location.yml"),
            'on: push\njobs:\n  "bad.key\\nnext": scalar\n',
        )

    message = str(exc.value)
    assert "\n" not in message
    assert r'$["jobs"]["bad.key\nnext"]' in message
    assert "jobs.bad.key" not in message
