"""Tests for repository-global and managed GitHub CI audit policy."""

from pathlib import Path

import pytest

from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci.audit import (
    SECRET_NAMES,
    audit_global_workflows,
    audit_managed_installation,
    direct_doc_lattice_invocations,
)
from doc_lattice.github_ci.filesystem import (
    MAX_CUMULATIVE_WORKFLOW_BYTES,
    MAX_WORKFLOW_BYTES,
    MAX_WORKFLOW_FILES,
    discover_workflows,
    inspect_installed_artifacts,
)
from doc_lattice.github_ci.identity import parse_repository
from doc_lattice.github_ci.model import InstalledArtifact, WorkflowDiscovery, WorkflowDocument
from doc_lattice.github_ci.render import CHECKOUT_REF, SETUP_UV_REF, render_managed_artifacts
from doc_lattice.github_ci.workflow_parser import parse_workflow


def _workflow(text: str, path: str = ".github/workflows/example.yml") -> WorkflowDocument:
    return parse_workflow(Path(path), text)


def _finding_codes(findings) -> set[str]:
    return {finding.code for finding in findings}


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("doc-lattice linear --exit-code", (("linear", False),)),
        (
            '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear --exit-code',
            (("linear", False),),
        ),
        (
            "uvx --from doc-lattice==2.1.0 doc-lattice reconcile target",
            (("reconcile", False),),
        ),
        (
            "uv run doc-lattice reconcile --all --dry-run",
            (("reconcile", True),),
        ),
        ("echo 'doc-lattice linear'", ()),
        ('printf "%s\\n" "doc-lattice reconcile --all"', ()),
        (
            "set +e\ndoc-lattice check\nrc_check=$?\ndoc-lattice lint\nrc_lint=$?\n",
            (("check", False), ("lint", False)),
        ),
        ("if doc-lattice linear; then printf ok; fi", (("linear", False),)),
    ],
)
def test_direct_doc_lattice_invocations_handles_documented_forms(script, expected):
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("TOKEN=value doc-lattice linear", (("linear", False),)),
        ("env TOKEN=value doc-lattice linear", (("linear", False),)),
        ("! doc-lattice reconcile --all --dry-run", (("reconcile", True),)),
        (
            "if true; then doc-lattice check; fi; while false; do doc-lattice lint; done",
            (("check", False), ("lint", False)),
        ),
        (
            "uvx --python 3.13 --from doc-lattice==2.1.0 doc-lattice check",
            (("check", False),),
        ),
        ("uv run --isolated -- doc-lattice lint", (("lint", False),)),
        (
            "doc-lattice check && (doc-lattice lint || doc-lattice reconcile --dry-run); "
            "doc-lattice linear",
            (
                ("check", False),
                ("lint", False),
                ("reconcile", True),
                ("linear", False),
            ),
        ),
        ("doc-lattice rec\\\noncile --all --dry-run", (("reconcile", True),)),
    ],
)
def test_direct_doc_lattice_invocations_handles_shell_prefixes_and_boundaries(script, expected):
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("PATH+=:/tools doc-lattice linear --exit-code", (("linear", False),)),
        (
            "FLAGS+=x uv run doc-lattice reconcile --all",
            (("reconcile", False),),
        ),
        (
            "FLAGS+=x uv run doc-lattice reconcile --all --dry-run",
            (("reconcile", True),),
        ),
    ],
)
def test_direct_doc_lattice_invocations_handles_bash_append_assignments(script, expected):
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    "script",
    [
        "other-doc-lattice linear",
        "doc-lattice-helper linear",
        "$RUNNER_TEMP/doc-lattice-helper linear",
        "echo doc-lattice linear",
        "printf doc-lattice reconcile",
        "runner doc-lattice linear",
        "+=x doc-lattice linear",
        "FLAGS++=x doc-lattice linear",
    ],
)
def test_direct_doc_lattice_invocations_ignores_indirect_or_similarly_named_commands(script):
    assert direct_doc_lattice_invocations(script) == ()


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice reconcile --dry-runner",
        "doc-lattice reconcile '--dry-run value'",
    ],
)
def test_direct_doc_lattice_invocations_requires_a_distinct_dry_run_token(script):
    assert direct_doc_lattice_invocations(script) == (("reconcile", False),)


def test_direct_doc_lattice_invocations_keeps_dry_run_scoped_to_one_command():
    script = "doc-lattice reconcile --all; doc-lattice check --dry-run"

    assert direct_doc_lattice_invocations(script) == (
        ("reconcile", False),
        ("check", True),
    )


def test_direct_doc_lattice_invocations_discards_only_malformed_fragment():
    script = "doc-lattice check; echo 'unterminated"

    assert direct_doc_lattice_invocations(script) == (("check", False),)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("uvx --with requests doc-lattice linear", (("linear", False),)),
        (
            "uvx --index https://packages.example/simple doc-lattice linear",
            (("linear", False),),
        ),
        (
            "uvx --index=https://packages.example/simple -w requests doc-lattice check",
            (("check", False),),
        ),
        (
            "uv run --group dev doc-lattice reconcile --all",
            (("reconcile", False),),
        ),
        (
            "uv run --group=dev --with requests doc-lattice reconcile --dry-run",
            (("reconcile", True),),
        ),
        ("command -p doc-lattice linear", (("linear", False),)),
        ("command -- doc-lattice check", (("check", False),)),
        ("exec -a lattice doc-lattice reconcile --all", (("reconcile", False),)),
        ("exec -c doc-lattice lint", (("lint", False),)),
        ("2>/dev/null doc-lattice linear", (("linear", False),)),
        ("</dev/null 3>&1 command doc-lattice check", (("check", False),)),
    ],
)
def test_direct_doc_lattice_invocations_handles_supported_wrappers_and_redirections(
    script,
    expected,
):
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    "script",
    [
        "command -v doc-lattice linear",
        "command -V doc-lattice linear",
        "command -pv doc-lattice linear",
        "uv run --module doc-lattice linear",
        "uv run --module=doc-lattice linear",
        "uv run -m doc-lattice linear",
        "uv run -mdoc-lattice linear",
        "uv run --script doc-lattice linear",
        "uv run -s doc-lattice linear",
        "uv run --gui-script doc-lattice linear",
    ],
)
def test_direct_doc_lattice_invocations_ignores_nonexecuting_command_forms(script):
    assert direct_doc_lattice_invocations(script) == ()


def test_direct_doc_lattice_invocations_ignores_heredoc_bodies():
    script = """\
doc-lattice check
cat <<'POLICY'
doc-lattice linear
uv run doc-lattice reconcile --all
POLICY
doc-lattice lint
"""

    assert direct_doc_lattice_invocations(script) == (
        ("check", False),
        ("lint", False),
    )


def test_direct_doc_lattice_invocations_keeps_command_with_and_after_heredoc():
    script = """\
doc-lattice check <<-EOF
	doc-lattice linear
	EOF
doc-lattice lint
"""

    assert direct_doc_lattice_invocations(script) == (
        ("check", False),
        ("lint", False),
    )


def test_direct_doc_lattice_invocations_keeps_command_after_here_string():
    script = "cat <<< harmless\ndoc-lattice linear"

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_keeps_command_after_arithmetic_shift():
    script = "(( x = 1 << 2 ))\ndoc-lattice reconcile --all"

    assert direct_doc_lattice_invocations(script) == (("reconcile", False),)


def test_direct_doc_lattice_invocations_assembles_quoted_heredoc_delimiter_word():
    script = """\
cat <<'E'OF
harmless
EOF
doc-lattice linear
"""

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_preserves_non_special_double_quote_escape():
    script = 'cat <<"E\\OF"\nharmless\nE\\OF\ndoc-lattice linear\n'

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_detects_legacy_command_substitution():
    script = "echo `doc-lattice linear`"

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_detects_legacy_substitution_in_double_quotes():
    script = 'echo "`doc-lattice linear`"'

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


@pytest.mark.parametrize(
    "script",
    [
        "echo '`doc-lattice linear`'",
        r"echo \`doc-lattice linear\`",
    ],
)
def test_direct_doc_lattice_invocations_ignores_literal_backticks(script):
    assert direct_doc_lattice_invocations(script) == ()


def test_direct_doc_lattice_invocations_keeps_command_after_comment_line():
    script = "# setup\ndoc-lattice linear"

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_keeps_command_after_trailing_comment():
    script = "echo setup # harmless\ndoc-lattice linear"

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_removes_ansi_c_quoted_heredoc_body():
    script = """\
cat <<$'EOF'
harmless
EOF
doc-lattice linear
"""

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_ignores_literal_backticks_in_substitution():
    script = '''echo "$(printf '%s' '`doc-lattice linear`')"'''

    assert direct_doc_lattice_invocations(script) == ()


def test_direct_doc_lattice_invocations_detects_active_backticks_in_substitution():
    script = '''echo "$(printf '%s' `doc-lattice linear`)"'''

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_ignores_heredoc_text_in_comment():
    script = "# note <<EOF\ndoc-lattice linear"

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_removes_locale_quoted_heredoc_body():
    script = """\
cat <<$"EOF"
harmless
EOF
doc-lattice linear
"""

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_keeps_hash_inside_shell_word():
    script = "doc-lattice reconcile --all --ref --dry-run#suffix"

    assert direct_doc_lattice_invocations(script) == (("reconcile", False),)


def test_direct_doc_lattice_invocations_tracks_parameter_expansion_in_substitution():
    script = '''echo "$(printf %s ${x:-)}; printf '%s' '`doc-lattice linear`')"'''

    assert direct_doc_lattice_invocations(script) == ()


def test_global_audit_reports_target_secret_linear_and_mutating_reconcile():
    document = _workflow(
        """\
name: unsafe
on:
  pull_request_target:
  pull_request_review:
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - env:
          TOKEN: ${{ secrets.LINEAR_API_KEY }}
        run: |
          doc-lattice linear --exit-code
          doc-lattice reconcile --all
"""
    )

    findings = audit_global_workflows((document,))

    assert _finding_codes(findings) == {
        "PULL_REQUEST_TARGET",
        "PR_LINEAR_INVOCATION",
        "PR_MUTATING_RECONCILE",
        "LINEAR_SECRET_REFERENCE",
    }


def test_global_audit_allows_unrelated_release_workflow():
    document = _workflow(
        """\
name: release
on:
  push:
    tags: ["v*"]
permissions:
  contents: write
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: true
      - uses: actions/cache@v4
        with:
          path: .cache
      - run: uv publish
"""
    )

    assert audit_global_workflows((document,)) == ()


@pytest.mark.parametrize(
    "event",
    [
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
    ],
)
def test_global_audit_applies_command_rules_to_every_pr_event(event: str):
    document = _workflow(
        f"""\
on:
  {event}:
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          doc-lattice linear
          uv run doc-lattice reconcile --all
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {
        "PR_LINEAR_INVOCATION",
        "PR_MUTATING_RECONCILE",
    }


def test_global_audit_allows_pr_dry_run_reconcile():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: doc-lattice reconcile --all --dry-run
"""
    )

    assert audit_global_workflows((document,)) == ()


def test_global_audit_does_not_apply_pr_command_rules_to_workflow_run():
    document = _workflow(
        """\
on:
  workflow_run:
    workflows: [CI]
    types: [completed]
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          doc-lattice linear
          doc-lattice reconcile --all
"""
    )

    assert audit_global_workflows((document,)) == ()


@pytest.mark.parametrize(
    "fragment",
    [
        "TOKEN: LINEAR_API_KEY",
        "TOKEN: DOC_LATTICE_LINEAR_API_KEY",
        "TOKEN: ${{ secrets.LINEAR_API_KEY }}",
        "TOKEN: ${{ secrets['DOC_LATTICE_LINEAR_API_KEY'] }}",
        "TOKEN: ${{ secrets [ 'LINEAR_API_KEY' ] }}",
        "TOKEN: reusable.yml?secret=DOC_LATTICE_LINEAR_API_KEY",
    ],
)
def test_global_audit_detects_linear_secret_names_in_scalar_syntaxes(fragment: str):
    document = _workflow(
        f"""\
on: workflow_call
jobs:
  reusable:
    runs-on: ubuntu-latest
    env:
      {fragment}
    steps:
      - uses: owner/reusable/.github/workflows/check.yml@main
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"LINEAR_SECRET_REFERENCE"}


@pytest.mark.parametrize("secret_name", sorted(SECRET_NAMES))
def test_global_audit_detects_secret_names_used_as_job_or_step_env_keys(secret_name: str):
    job_env = _workflow(
        f"""\
on: push
jobs:
  audit:
    runs-on: ubuntu-latest
    env:
      {secret_name}: ordinary
    steps:
      - run: true
"""
    )
    step_env = _workflow(
        f"""\
on: push
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - env:
          {secret_name}: ordinary
        run: true
"""
    )

    assert _finding_codes(audit_global_workflows((job_env,))) == {"LINEAR_SECRET_REFERENCE"}
    assert _finding_codes(audit_global_workflows((step_env,))) == {"LINEAR_SECRET_REFERENCE"}


def test_global_audit_allows_only_the_exact_canonical_linear_secret_slot():
    canonical = _workflow(
        """\
on: push
jobs:
  linear:
    runs-on: ubuntu-latest
    steps:
      - run: install
      - env:
          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}
        run: doc-lattice linear --exit-code
""",
        ".github/workflows/doc-lattice-linear.yml",
    )
    duplicate = _workflow(
        """\
on: push
jobs:
  linear:
    runs-on: ubuntu-latest
    env:
      TOKEN: DOC_LATTICE_LINEAR_API_KEY
    steps:
      - run: install
      - env:
          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}
        run: doc-lattice linear --exit-code
""",
        ".github/workflows/doc-lattice-linear.yml",
    )

    assert audit_global_workflows((canonical,)) == ()
    assert _finding_codes(audit_global_workflows((duplicate,))) == {"LINEAR_SECRET_REFERENCE"}


@pytest.mark.parametrize(
    "value",
    [
        "${{ secrets.linear_api_key }}",
        "${{ secrets['Doc_Lattice_Linear_Api_Key'] }}",
        "${{ toJSON(SeCrEtS.LINEAR_API_KEY) }}",
        "${{ secrets[env.linear_api_key] }}",
    ],
)
def test_global_audit_detects_case_insensitive_secret_references(value: str):
    document = _workflow(
        f"""\
on: push
env:
  TOKEN: {value}
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: true
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"LINEAR_SECRET_REFERENCE"}


@pytest.mark.parametrize("key", ["LINEAR_API_KEY", "doc_lattice_linear_api_key"])
def test_global_audit_detects_root_environment_secret_keys(key: str):
    document = _workflow(
        f"""\
on: push
env:
  {key}: ordinary
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: true
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"LINEAR_SECRET_REFERENCE"}


def test_global_audit_rejects_case_variation_in_canonical_secret_slot():
    document = _workflow(
        """\
on: push
jobs:
  linear:
    runs-on: ubuntu-latest
    steps:
      - run: install
      - env:
          linear_api_key: ${{ secrets.doc_lattice_linear_api_key }}
        run: doc-lattice linear --exit-code
""",
        ".github/workflows/doc-lattice-linear.yml",
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"LINEAR_SECRET_REFERENCE"}


def test_global_audit_deduplicates_identical_findings_with_stable_details():
    document = _workflow(
        """\
on: [pull_request, pull_request_target]
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: doc-lattice linear
""",
        ".github/workflows/duplicate.yml",
    )

    findings = audit_global_workflows((document, document))

    assert len(findings) == 2
    assert [(finding.path, finding.code, finding.message) for finding in findings] == [
        (
            ".github/workflows/duplicate.yml",
            "PR_LINEAR_INVOCATION",
            "pull-request workflows must not invoke doc-lattice linear",
        ),
        (
            ".github/workflows/duplicate.yml",
            "PULL_REQUEST_TARGET",
            "pull_request_target is prohibited for repository workflows",
        ),
    ]


def test_global_audit_documents_arbitrary_script_indirection_as_undetected():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: ./scripts/run-doc-policy
"""
    )

    assert audit_global_workflows((document,)) == ()


def _write_managed_artifacts(root: Path, repository: str = "Guardantix/doc-lattice") -> None:
    for artifact in render_managed_artifacts(repository, "2.1.0"):
        destination = root / artifact.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(artifact.text, encoding="utf-8")


def test_discover_workflows_returns_normal_absent_directory_state(tmp_path: Path):
    discovery = discover_workflows(tmp_path)

    assert discovery == WorkflowDiscovery(directory_exists=False, documents=())
    assert not (tmp_path / ".github").exists()


@pytest.mark.parametrize("kind", ["external", "internal", "broken"])
def test_discover_workflows_rejects_symlinked_github_parent_when_workflows_absent(
    tmp_path: Path,
    kind: str,
):
    root = tmp_path / "root"
    root.mkdir()
    if kind == "external":
        target = tmp_path / "outside"
        target.mkdir()
    elif kind == "internal":
        target = root / "internal"
        target.mkdir()
    else:
        target = root / "missing"
    (root / ".github").symlink_to(target, target_is_directory=True)

    with pytest.raises(ConfigError) as caught:
        discover_workflows(root)

    assert ".github/workflows" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_discover_workflows_reads_direct_yaml_files_in_stable_relative_order(
    tmp_path: Path,
):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "z.yaml").write_text("on: push\njobs: {}\n", encoding="utf-8")
    (workflows / "a.yml").write_text("on: pull_request\njobs: {}\n", encoding="utf-8")
    (workflows / "ignored.txt").write_text("not yaml", encoding="utf-8")
    nested = workflows / "nested"
    nested.mkdir()
    (nested / "nested.yml").write_text("on: push\njobs: {}\n", encoding="utf-8")

    discovery = discover_workflows(tmp_path)

    assert discovery.directory_exists is True
    assert [document.path for document in discovery.documents] == [
        Path(".github/workflows/a.yml"),
        Path(".github/workflows/z.yaml"),
    ]
    assert [trigger.name for document in discovery.documents for trigger in document.triggers] == [
        "pull_request",
        "push",
    ]


def test_discover_workflows_rejects_workflows_directory_file(tmp_path: Path):
    github = tmp_path / ".github"
    github.mkdir()
    (github / "workflows").write_text("not a directory", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"workflow directory.*directory"):
        discover_workflows(tmp_path)


def test_discover_workflows_rejects_external_workflows_directory_symlink(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "unsafe.yml").write_text("on: push\njobs: {}\n", encoding="utf-8")
    root = tmp_path / "root"
    (root / ".github").mkdir(parents=True)
    (root / ".github/workflows").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigError, match=r"symlink.*workflow directory"):
        discover_workflows(root)


def test_discover_workflows_rejects_symlinked_or_nonregular_yaml_files(tmp_path: Path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    real = tmp_path / "real.yml"
    real.write_text("on: push\njobs: {}\n", encoding="utf-8")
    (workflows / "linked.yml").symlink_to(real)

    with pytest.raises(ConfigError, match=r"symlink.*\.github/workflows/linked\.yml"):
        discover_workflows(tmp_path)

    (workflows / "linked.yml").unlink()
    (workflows / "directory.yaml").mkdir()

    with pytest.raises(ConfigError, match=r"regular file.*directory\.yaml"):
        discover_workflows(tmp_path)


def test_discover_workflows_rejects_non_utf8_or_malformed_yaml(tmp_path: Path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    target = workflows / "bad.yml"
    target.write_bytes(b"\xff\xfe")

    with pytest.raises(ConfigError, match=r"UTF-8.*\.github/workflows/bad\.yml"):
        discover_workflows(tmp_path)

    target.write_text("on: [", encoding="utf-8")

    with pytest.raises(
        ConfigError,
        match=r'cannot parse GitHub workflow "\.github/workflows/bad\.yml"',
    ):
        discover_workflows(tmp_path)


def test_discover_workflows_rejects_more_than_maximum_direct_files(tmp_path: Path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    for index in range(MAX_WORKFLOW_FILES + 1):
        (workflows / f"{index:03}.yml").write_text("jobs: {}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"more than 256.*\.github/workflows"):
        discover_workflows(tmp_path)


def test_discover_workflows_stops_enumerating_at_file_count_limit(
    tmp_path: Path,
    monkeypatch,
):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    real_iterdir = Path.iterdir

    def _bounded_entries(path: Path):
        if path != workflows:
            yield from real_iterdir(path)
            return
        for index in range(MAX_WORKFLOW_FILES + 1):
            yield path / f"{index:03}.yml"
        raise AssertionError("workflow discovery consumed past its declared file limit")

    monkeypatch.setattr(Path, "iterdir", _bounded_entries)

    with pytest.raises(ConfigError, match=r"more than 256.*\.github/workflows"):
        discover_workflows(tmp_path)


def test_discover_workflows_rejects_per_file_byte_limit_plus_one(tmp_path: Path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    target = workflows / "large.yml"
    target.write_bytes(b"jobs: {}\n#" + b"x" * (MAX_WORKFLOW_BYTES - 9))
    assert target.stat().st_size == MAX_WORKFLOW_BYTES + 1

    with pytest.raises(ConfigError, match=r"byte limit.*\.github/workflows/large\.yml"):
        discover_workflows(tmp_path)


def test_discover_workflows_rejects_cumulative_byte_limit_plus_one(tmp_path: Path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    for index in range(8):
        target = workflows / f"{index}.yml"
        target.write_bytes(b"jobs: {}\n#" + b"x" * (MAX_WORKFLOW_BYTES - 10))
        assert target.stat().st_size == MAX_WORKFLOW_BYTES
    (workflows / "overflow.yml").write_bytes(b"x")
    assert sum(path.stat().st_size for path in workflows.iterdir()) == (
        MAX_CUMULATIVE_WORKFLOW_BYTES + 1
    )

    with pytest.raises(ConfigError, match=r"cumulative byte limit.*\.github/workflows"):
        discover_workflows(tmp_path)


def test_discover_workflows_rejects_file_growth_between_stat_and_read(
    tmp_path: Path,
    monkeypatch,
):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    target = workflows / "growing.yml"
    target.write_text("jobs: {}\n", encoding="utf-8")
    real_open = Path.open
    grew = False

    def _grow_after_open(path: Path, *args, **kwargs):
        nonlocal grew
        handle = real_open(path, *args, **kwargs)
        if path == target and not grew and "r" in args[0]:
            grew = True
            with real_open(path, "ab") as writer:
                writer.write(b"# grew\n")
        return handle

    monkeypatch.setattr(Path, "open", _grow_after_open)

    with pytest.raises(ConfigError, match=r"changed during discovery.*growing\.yml"):
        discover_workflows(tmp_path)


def test_inspect_installed_artifacts_returns_exact_text_and_parsed_markers(tmp_path: Path):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_managed_artifacts(tmp_path)

    installed = inspect_installed_artifacts(tmp_path, expected)

    assert all(isinstance(artifact, InstalledArtifact) for artifact in installed)
    assert [artifact.expected for artifact in installed if artifact is not None] == list(expected)
    assert [artifact.text for artifact in installed if artifact is not None] == [
        artifact.text for artifact in expected
    ]
    assert [artifact.marker.role for artifact in installed if artifact and artifact.marker] == [
        "offline",
        "linear",
        "bootstrap",
    ]
    assert all(artifact.marker_error is None for artifact in installed if artifact)


def test_inspect_installed_artifacts_preserves_missing_positions_without_mutation(
    tmp_path: Path,
):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    offline = expected[0]
    destination = tmp_path / offline.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_text(offline.text, encoding="utf-8")

    installed = inspect_installed_artifacts(tmp_path, expected)

    assert installed[0] is not None
    assert installed[1:] == (None, None)
    assert not (tmp_path / ".github/doc-lattice-bootstrap.sh").exists()


def test_inspect_installed_artifacts_reports_bad_marker_as_data(tmp_path: Path):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    bootstrap = expected[-1]
    destination = tmp_path / bootstrap.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_text(
        bootstrap.text.replace(
            "# doc-lattice-managed: github-ci-v1",
            "# doc-lattice-managed: broken",
            1,
        ),
        encoding="utf-8",
    )

    installed = inspect_installed_artifacts(tmp_path, expected)

    assert installed[:2] == (None, None)
    assert installed[2] is not None
    assert installed[2].marker is None
    assert "invalid ownership marker" in (installed[2].marker_error or "")


def test_inspect_installed_artifacts_rejects_non_utf8_and_symlinks(tmp_path: Path):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    offline = expected[0]
    destination = tmp_path / offline.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"\xff\xfe")

    with pytest.raises(ConfigError, match=r"UTF-8.*doc-lattice\.yml"):
        inspect_installed_artifacts(tmp_path, expected)

    destination.unlink()
    real = tmp_path / "real.yml"
    real.write_text(offline.text, encoding="utf-8")
    destination.symlink_to(real)

    with pytest.raises(ConfigError, match=r"symlink.*doc-lattice\.yml"):
        inspect_installed_artifacts(tmp_path, expected)


def test_inspect_installed_artifacts_rejects_oversized_managed_file(tmp_path: Path):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    offline = expected[0]
    destination = tmp_path / offline.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"x" * (MAX_WORKFLOW_BYTES + 1))

    with pytest.raises(ConfigError, match=r"byte limit.*doc-lattice\.yml"):
        inspect_installed_artifacts(tmp_path, expected)


def test_inspect_external_parent_error_uses_only_repository_relative_path(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / ".github").symlink_to(outside, target_is_directory=True)
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")

    with pytest.raises(ConfigError) as caught:
        inspect_installed_artifacts(root, expected)

    assert ".github/workflows/doc-lattice.yml" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_inspection_never_yaml_parses_bootstrap(tmp_path: Path):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    bootstrap = expected[-1]
    destination = tmp_path / bootstrap.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_text(bootstrap.text + "\nnot: [valid YAML\n", encoding="utf-8")

    installed = inspect_installed_artifacts(tmp_path, expected)

    assert installed[2] is not None
    assert installed[2].marker is not None


def _audit_installed(
    root: Path,
    *,
    expected_repository: str = "Guardantix/doc-lattice",
    running_version: str = "2.1.0",
):
    expected = render_managed_artifacts(expected_repository, running_version)
    discovery = discover_workflows(root)
    installed = inspect_installed_artifacts(root, expected)
    findings = audit_managed_installation(
        discovery,
        installed,
        parse_repository(expected_repository),
        running_version,
    )
    return findings


def _mutate_artifact(
    root: Path,
    role: str,
    old: str,
    new: str,
) -> None:
    artifact = next(
        artifact
        for artifact in render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
        if artifact.role == role
    )
    destination = root / artifact.relative_path
    text = destination.read_text(encoding="utf-8")
    assert old in text
    destination.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_managed_audit_accepts_exact_rendered_installation(tmp_path: Path):
    _write_managed_artifacts(tmp_path)

    assert _audit_installed(tmp_path) == ()


def test_managed_audit_requires_exactly_three_canonical_inspection_slots(tmp_path: Path):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    discovery = discover_workflows(tmp_path)
    installed = inspect_installed_artifacts(tmp_path, expected)

    with pytest.raises(ConfigError, match="exactly three"):
        audit_managed_installation(
            discovery,
            installed[:2],
            parse_repository("Guardantix/doc-lattice"),
            "2.1.0",
        )


def test_managed_audit_rejects_present_artifacts_out_of_canonical_order(tmp_path: Path):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_managed_artifacts(tmp_path)
    discovery = discover_workflows(tmp_path)
    installed = inspect_installed_artifacts(tmp_path, expected)

    with pytest.raises(ConfigError, match="canonical order"):
        audit_managed_installation(
            discovery,
            tuple(reversed(installed)),
            parse_repository("Guardantix/doc-lattice"),
            "2.1.0",
        )


def test_managed_audit_reports_absent_directory_and_artifacts_as_findings(tmp_path: Path):
    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {
        "MISSING_WORKFLOW_DIRECTORY",
        "MISSING_MANAGED_ARTIFACT",
    }
    assert not (tmp_path / ".github").exists()


@pytest.mark.parametrize("missing_role", ["offline", "linear", "bootstrap"])
def test_managed_audit_reports_each_missing_canonical_artifact(
    tmp_path: Path,
    missing_role: str,
):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_managed_artifacts(tmp_path)
    missing = next(artifact for artifact in artifacts if artifact.role == missing_role)
    (tmp_path / missing.relative_path).unlink()

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {"MISSING_MANAGED_ARTIFACT"}
    assert {finding.path for finding in findings} == {missing.relative_path.as_posix()}


def test_managed_audit_reports_stale_generator_without_current_version_cascade(
    tmp_path: Path,
):
    _write_managed_artifacts(tmp_path)
    old_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")
    for artifact in old_artifacts:
        destination = tmp_path / artifact.relative_path
        destination.write_text(artifact.text, encoding="utf-8")

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {"STALE_GENERATOR"}
    assert all("ci refresh" in finding.message for finding in findings)


def test_managed_audit_reports_invalid_bootstrap_marker_as_finding(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(
        tmp_path,
        "bootstrap",
        "# doc-lattice-managed: github-ci-v1",
        "# doc-lattice-managed: broken",
    )

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {"MANAGED_MARKER"}
    assert findings[0].path == ".github/doc-lattice-bootstrap.sh"


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("branches: [main]", "branches: [develop]", "MANAGED_TRIGGERS"),
        ("contents: read", "contents: write", "MANAGED_PERMISSIONS"),
        (
            f"actions/checkout@{CHECKOUT_REF}",
            "actions/checkout@v4",
            "MANAGED_ACTION",
        ),
        ("persist-credentials: false", "persist-credentials: true", "MANAGED_CHECKOUT"),
        ("enable-cache: false", "enable-cache: true", "MANAGED_CACHE"),
        ("doc-lattice check", "doc-lattice check-changed", "MANAGED_COMMAND"),
    ],
)
def test_managed_audit_reports_focused_offline_drift(
    tmp_path: Path,
    old: str,
    new: str,
    code: str,
):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(tmp_path, "offline", old, new)

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {code}
    assert {finding.path for finding in findings} == {".github/workflows/doc-lattice.yml"}


def test_managed_audit_rejects_actions_cache_in_managed_offline_workflow(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(
        tmp_path,
        "offline",
        "      - name: Audit, check, and lint\n",
        "      - uses: actions/cache@v4\n"
        "        with:\n"
        "          path: .cache\n"
        "      - name: Audit, check, and lint\n",
    )

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {"MANAGED_CACHE"}


def test_managed_audit_detects_action_moved_after_command_step(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    offline = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")[0]
    destination = tmp_path / offline.relative_path
    text = destination.read_text(encoding="utf-8")
    setup_uv = (
        f"      - uses: astral-sh/setup-uv@{SETUP_UV_REF} # v6.8.0\n"
        "        with:\n"
        "          enable-cache: false\n"
    )
    assert setup_uv in text
    destination.write_text(
        text.replace(setup_uv, "", 1).rstrip() + "\n" + setup_uv,
        encoding="utf-8",
    )

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {"MANAGED_ACTION"}


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("jobs:\n  linear:", "jobs:\n  trusted:", "MANAGED_JOB"),
        (
            "github.repository == 'Guardantix/doc-lattice'",
            "github.repository == 'other/repository'",
            "MANAGED_COMMAND",
        ),
        (
            "environment: doc-lattice-linear",
            "environment: production",
            "MANAGED_JOB",
        ),
        (
            "LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}",
            "TOKEN: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}",
            "MANAGED_SECRET",
        ),
        (
            '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear --exit-code',
            '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" lint',
            "MANAGED_COMMAND",
        ),
        (
            "uv python install 3.13\n"
            '          uv venv --python 3.13 "$RUNNER_TEMP/doc-lattice-venv"',
            'uv venv --python 3.13 "$RUNNER_TEMP/doc-lattice-venv"\n'
            "          uv python install 3.13",
            "MANAGED_COMMAND",
        ),
    ],
)
def test_managed_audit_reports_focused_linear_drift(
    tmp_path: Path,
    old: str,
    new: str,
    code: str,
):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(tmp_path, "linear", old, new)

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {code}
    assert {finding.path for finding in findings} == {".github/workflows/doc-lattice-linear.yml"}


def test_managed_audit_requires_linear_secret_only_on_final_step(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(
        tmp_path,
        "linear",
        "      - name: Run trusted Linear gate\n"
        "        env:\n"
        "          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}\n"
        "        run:",
        "      - env:\n"
        "          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}\n"
        "        run: echo early\n"
        "      - name: Run trusted Linear gate\n"
        "        run:",
    )

    findings = _audit_installed(tmp_path)

    assert "MANAGED_SECRET" in _finding_codes(findings)


def test_managed_audit_accepts_ascii_case_only_repository_identity_change(tmp_path: Path):
    _write_managed_artifacts(tmp_path, "guardantix/DOC-LATTICE")

    findings = _audit_installed(
        tmp_path,
        expected_repository="Guardantix/doc-lattice",
    )

    assert findings == ()


def test_managed_audit_reports_repository_rename_without_semantic_cascade(tmp_path: Path):
    _write_managed_artifacts(tmp_path, "FormerOwner/former-repository")

    findings = _audit_installed(
        tmp_path,
        expected_repository="Guardantix/doc-lattice",
    )

    assert _finding_codes(findings) == {"REPOSITORY_IDENTITY"}


def test_managed_audit_ignores_unrelated_workflow_permissions(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    release = tmp_path / ".github/workflows/release.yml"
    release.write_text(
        """\
on:
  push:
    tags: ["v*"]
permissions:
  contents: write
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
""",
        encoding="utf-8",
    )

    assert _audit_installed(tmp_path) == ()


def test_managed_audit_findings_are_sorted_and_unique(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(tmp_path, "offline", "contents: read", "contents: write")
    _mutate_artifact(tmp_path, "linear", "contents: read", "contents: write")

    findings = _audit_installed(tmp_path)

    assert findings == tuple(sorted(set(findings)))


@pytest.mark.parametrize(
    ("role", "old", "new", "code"),
    [
        (
            "offline",
            "  pull_request:\n    branches: [main]",
            "  pull_request:\n    branches: [main]\n    paths: [docs/**]",
            "MANAGED_TRIGGERS",
        ),
        (
            "offline",
            "  pull_request:\n    branches: [main]",
            "  pull_request:\n    branches: [main]\n    paths-ignore: [generated/**]",
            "MANAGED_TRIGGERS",
        ),
        (
            "offline",
            "permissions:\n  contents: read",
            "env:\n"
            "  UV_DEFAULT_INDEX: https://example.invalid/simple\n"
            "permissions:\n"
            "  contents: read",
            "MANAGED_COMMAND",
        ),
        (
            "offline",
            "permissions:\n  contents: read",
            "defaults:\n"
            "  run:\n"
            "    shell: bash\n"
            "    working-directory: docs\n"
            "permissions:\n"
            "  contents: read",
            "MANAGED_COMMAND",
        ),
        (
            "offline",
            "      - name: Audit, check, and lint\n        run:",
            "      - name: Audit, check, and lint\n        if: false\n"
            "        continue-on-error: true\n        shell: bash\n"
            "        working-directory: docs\n        run:",
            "MANAGED_COMMAND",
        ),
        (
            "offline",
            "    runs-on: ubuntu-latest",
            "    if: false\n    continue-on-error: true\n    runs-on: ubuntu-latest",
            "MANAGED_COMMAND",
        ),
        (
            "offline",
            "      - name: Audit, check, and lint",
            "      - uses: owner/extra-action@main\n      - name: Audit, check, and lint",
            "MANAGED_ACTION",
        ),
        (
            "offline",
            "      - name: Audit, check, and lint",
            "      - run: echo extra\n      - name: Audit, check, and lint",
            "MANAGED_COMMAND",
        ),
        (
            "linear",
            "      - name: Run trusted Linear gate\n"
            "        env:\n"
            "          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}\n"
            '        run: \'"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" '
            "linear --exit-code'\n",
            "",
            "MANAGED_COMMAND",
        ),
    ],
)
def test_managed_audit_detects_residual_behavioral_structure(
    tmp_path: Path,
    role: str,
    old: str,
    new: str,
    code: str,
):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(tmp_path, role, old, new)

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {code}


def test_managed_audit_allows_display_name_changes(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(tmp_path, "offline", "name: doc-lattice", "name: Friendly workflow")
    _mutate_artifact(
        tmp_path,
        "offline",
        "name: Offline doc-lattice gates",
        "name: Friendly job",
    )
    _mutate_artifact(
        tmp_path,
        "offline",
        "name: Audit, check, and lint",
        "name: Friendly step",
    )

    assert _audit_installed(tmp_path) == ()
