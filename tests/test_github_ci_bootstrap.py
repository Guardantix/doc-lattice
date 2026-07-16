"""Tests for the human-run GitHub environment bootstrap script."""

import json
import os
import pty
import subprocess
from pathlib import Path

import pytest

from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci.render import (
    BOOTSTRAP_PATH,
    render_bootstrap,
    render_managed_artifacts,
)

REPOSITORY_JQ = "[.full_name, .default_branch, .visibility, .owner.type] | @tsv"
PLAN_JQ = ".plan.name"
REPOSITORY_SECRETS_JQ = ".secrets[].name"
ENVIRONMENTS_JQ = ".environments[].name"
ENVIRONMENT_JQ = (
    "[.deployment_branch_policy.custom_branch_policies, "
    ".deployment_branch_policy.protected_branches] | @tsv"
)
POLICIES_JQ = ".branch_policies[] | [.name, .type] | @tsv"
ENVIRONMENT_SECRETS_JQ = ".secrets[].name"

_FAKE_GH = r"""#!/usr/bin/env python3
import json
import os
import sys

state_path = os.environ["FAKE_GH_STATE"]
log_path = os.environ["FAKE_GH_LOG"]
with open(state_path, encoding="utf-8") as stream:
    state = json.load(stream)

argv = sys.argv[1:]
with open(log_path, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(argv) + "\n")

def save():
    with open(state_path, "w", encoding="utf-8") as stream:
        json.dump(state, stream, sort_keys=True)

def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)

if argv == ["auth", "status", "--hostname", "github.com"]:
    raise SystemExit(0 if state.get("auth_ok", True) else 1)

if argv[:3] != ["api", "--hostname", "github.com"]:
    fail("unexpected fake gh command")

arguments = argv[3:]
repository = state["repository"]
full_name = repository["full_name"]
owner, name = full_name.split("/", 1)
repo_endpoint = f"repos/{full_name}"
environment_endpoint = f"{repo_endpoint}/environments/doc-lattice-linear"

if arguments == [repo_endpoint, "--jq", os.environ["FAKE_REPOSITORY_JQ"]] or (
    len(arguments) == 3
    and arguments[0].lower() == repo_endpoint.lower()
    and arguments[1:] == ["--jq", os.environ["FAKE_REPOSITORY_JQ"]]
):
    state["repository_reads"] = state.get("repository_reads", 0) + 1
    if state["repository_reads"] == 2 and "canonical_on_reinspection" in state:
        repository["full_name"] = state["canonical_on_reinspection"]
    if state["repository_reads"] == 2 and "default_branch_on_reinspection" in state:
        repository["default_branch"] = state["default_branch_on_reinspection"]
    print(
        "\t".join(
            [
                repository["full_name"],
                repository["default_branch"],
                repository["visibility"],
                repository["owner_type"],
            ]
        )
    )
    if state["repository_reads"] == 2 and "change_on_reinspection" in state:
        state["environment"] = state["change_on_reinspection"]
    save()
    raise SystemExit(0)

plan_endpoint = f"users/{owner}" if repository["owner_type"] == "User" else f"orgs/{owner}"
if arguments == [plan_endpoint, "--jq", os.environ["FAKE_PLAN_JQ"]]:
    if state.get("plan_error", False):
        fail("plan metadata unavailable")
    state["plan_reads"] = state.get("plan_reads", 0) + 1
    if state["plan_reads"] == 2 and "plan_on_reinspection" in state:
        state["plan"] = state["plan_on_reinspection"]
    print(state.get("plan", ""))
    save()
    raise SystemExit(0)

if arguments == [
    f"{repo_endpoint}/actions/secrets",
    "--paginate",
    "--jq",
    os.environ["FAKE_REPOSITORY_SECRETS_JQ"],
]:
    if state.get("repo_secret_error", False):
        fail("repository secret metadata unavailable")
    print("\n".join(state.get("repo_secrets", [])))
    raise SystemExit(0)

if arguments == [
    f"{repo_endpoint}/environments",
    "--paginate",
    "--jq",
    os.environ["FAKE_ENVIRONMENTS_JQ"],
]:
    names = (
        [state.get("environment_name", "doc-lattice-linear")]
        if state.get("environment") is not None
        else []
    )
    names.extend(state.get("other_environments", []))
    print("\n".join(names))
    raise SystemExit(0)

environment = state.get("environment")
if (
    len(arguments) == 3
    and arguments[0].lower() == environment_endpoint.lower()
    and arguments[1:] == ["--jq", os.environ["FAKE_ENVIRONMENT_JQ"]]
):
    if environment is None:
        fail("environment missing")
    print(
        f'{str(environment["custom"]).lower()}\t'
        f'{str(environment["protected"]).lower()}'
    )
    raise SystemExit(0)

if (
    len(arguments) == 4
    and arguments[0].lower()
    == f"{environment_endpoint}/deployment-branch-policies".lower()
    and arguments[1:]
    == [
        "--paginate",
        "--jq",
        os.environ["FAKE_POLICIES_JQ"],
    ]
):
    if environment is None:
        fail("environment missing")
    print("\n".join(f'{row["name"]}\t{row["type"]}' for row in environment["policies"]))
    raise SystemExit(0)

if (
    len(arguments) == 4
    and arguments[0].lower() == f"{environment_endpoint}/secrets".lower()
    and arguments[1:]
    == [
        "--paginate",
        "--jq",
        os.environ["FAKE_ENVIRONMENT_SECRETS_JQ"],
    ]
):
    if environment is None:
        fail("environment missing")
    if state.get("environment_secret_error_after_mutation", False) and state.get(
        "mutation_count", 0
    ):
        fail("configured post-mutation environment secret inspection failure")
    print("\n".join(environment["secrets"]))
    raise SystemExit(0)

if (
    len(arguments) == 7
    and arguments[:2] == ["--method", "PUT"]
    and arguments[2].lower() == environment_endpoint.lower()
    and arguments[3:]
    == [
        "--field",
        "deployment_branch_policy[protected_branches]=false",
        "--field",
        "deployment_branch_policy[custom_branch_policies]=true",
    ]
):
    if state.get("put_error", False):
        fail("configured PUT failure")
    state["environment"] = {
        "custom": True,
        "protected": False,
        "policies": [],
        "secrets": [],
    }
    state["mutation_count"] = state.get("mutation_count", 0) + 1
    save()
    print("{}")
    raise SystemExit(0)

if (
    len(arguments) == 7
    and arguments[:2] == ["--method", "POST"]
    and arguments[2].lower()
    == f"{environment_endpoint}/deployment-branch-policies".lower()
    and arguments[3:]
    == [
        "--field",
        "name=main",
        "--field",
        "type=branch",
    ]
):
    if state.get("post_error", False):
        fail("configured POST failure")
    if environment is None:
        fail("environment missing")
    environment["policies"].append({"name": "main", "type": "branch"})
    state["mutation_count"] = state.get("mutation_count", 0) + 1
    save()
    print("{}")
    raise SystemExit(0)

fail("unexpected fake gh api argv: " + json.dumps(arguments))
"""

_FAKE_SORT = r"""#!/usr/bin/env python3
import os
import sys

count_path = os.environ["FAKE_SORT_COUNT"]
try:
    with open(count_path, encoding="utf-8") as stream:
        count = int(stream.read()) + 1
except FileNotFoundError:
    count = 1
with open(count_path, "w", encoding="utf-8") as stream:
    stream.write(str(count))

failures = {
    int(value)
    for value in os.environ.get("FAKE_SORT_FAIL_CALLS", "").split(",")
    if value
}
data = sys.stdin.read()
if count in failures:
    print(f"configured sort failure on call {count}", file=sys.stderr)
    raise SystemExit(1)
if data:
    sys.stdout.write("\n".join(sorted(data.splitlines())) + "\n")
"""

_FAKE_TR = r"""#!/usr/bin/env python3
import os
import sys

count_path = os.environ["FAKE_TR_COUNT"]
try:
    with open(count_path, encoding="utf-8") as stream:
        count = int(stream.read()) + 1
except FileNotFoundError:
    count = 1
with open(count_path, "w", encoding="utf-8") as stream:
    stream.write(str(count))

failures = {
    int(value)
    for value in os.environ.get("FAKE_TR_FAIL_CALLS", "").split(",")
    if value
}
if count in failures:
    print(f"configured tr failure on call {count}", file=sys.stderr)
    raise SystemExit(1)

accepted = [
    ["[:upper:]", "[:lower:]"],
    ["ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"],
]
if sys.argv[1:] not in accepted:
    print("unexpected fake tr argv", file=sys.stderr)
    raise SystemExit(1)
source = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
target = "abcdefghijklmnopqrstuvwxyz"
sys.stdout.write(sys.stdin.read().translate(str.maketrans(source, target)))
"""


def _exact_environment(*, secret_present: bool = True):
    """Build target-environment metadata without a secret value."""
    return {
        "custom": True,
        "protected": False,
        "policies": [{"name": "main", "type": "branch"}],
        "secrets": ["DOC_LATTICE_LINEAR_API_KEY"] if secret_present else [],
    }


def _state(**overrides):
    """Build one fake GitHub metadata state."""
    state = {
        "repository": {
            "full_name": "Guardantix/doc-lattice",
            "default_branch": "main",
            "visibility": "public",
            "owner_type": "Organization",
        },
        "environment": _exact_environment(),
        "environment_name": "doc-lattice-linear",
        "repo_secrets": [],
    }
    state.update(overrides)
    return state


def _write_fake_tools(tmp_path: Path):
    """Create strict metadata-only fake gh and sort executables."""
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    gh_path = bin_path / "gh"
    gh_path.write_text(_FAKE_GH)
    gh_path.chmod(0o755)
    sort_path = bin_path / "sort"
    sort_path.write_text(_FAKE_SORT)
    sort_path.chmod(0o755)
    tr_path = bin_path / "tr"
    tr_path.write_text(_FAKE_TR)
    tr_path.chmod(0o755)
    return bin_path


def _run_bootstrap(  # noqa: PLR0913
    tmp_path: Path,
    state,
    operation: str = "plan",
    repository: str = "Guardantix/doc-lattice",
    *,
    confirmation: str | None = None,
    include_gh: bool = True,
    arguments: list[str] | None = None,
    sort_fail_calls: tuple[int, ...] = (),
    tr_fail_calls: tuple[int, ...] = (),
):
    """Render and exercise the bootstrap against fake GitHub metadata."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    bootstrap = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")[2]
    script_path = tmp_path / "bootstrap.sh"
    script_path.write_text(bootstrap.text)
    script_path.chmod(0o755)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state, sort_keys=True))
    log_path = tmp_path / "gh.jsonl"
    log_path.write_text("")
    if include_gh:
        bin_path = _write_fake_tools(tmp_path)
        path = f"{bin_path}:{os.environ['PATH']}"
    else:
        bin_path = tmp_path / "missing-bin"
        bin_path.mkdir()
        (bin_path / "tr").symlink_to("/usr/bin/tr")
        path = str(bin_path)
    env = {
        **os.environ,
        "PATH": path,
        "LINEAR_SECRET_FIXTURE": "lin_api_DO_NOT_LEAK_FROM_BOOTSTRAP",  # pragma: allowlist secret
        "FAKE_GH_STATE": str(state_path),
        "FAKE_GH_LOG": str(log_path),
        "FAKE_SORT_COUNT": str(tmp_path / "sort-count"),
        "FAKE_SORT_FAIL_CALLS": ",".join(str(call) for call in sort_fail_calls),
        "FAKE_TR_COUNT": str(tmp_path / "tr-count"),
        "FAKE_TR_FAIL_CALLS": ",".join(str(call) for call in tr_fail_calls),
        "FAKE_REPOSITORY_JQ": REPOSITORY_JQ,
        "FAKE_PLAN_JQ": PLAN_JQ,
        "FAKE_REPOSITORY_SECRETS_JQ": REPOSITORY_SECRETS_JQ,
        "FAKE_ENVIRONMENTS_JQ": ENVIRONMENTS_JQ,
        "FAKE_ENVIRONMENT_JQ": ENVIRONMENT_JQ,
        "FAKE_POLICIES_JQ": POLICIES_JQ,
        "FAKE_ENVIRONMENT_SECRETS_JQ": ENVIRONMENT_SECRETS_JQ,
    }
    command = [
        "/bin/bash",
        str(script_path),
        *(arguments if arguments is not None else [operation, repository]),
    ]
    if confirmation is None:
        result = subprocess.run(  # noqa: S603
            command,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    else:
        master, slave = pty.openpty()
        process = subprocess.Popen(  # noqa: S603
            command,
            env=env,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(slave)
        os.write(master, f"{confirmation}\n".encode())
        stdout, stderr = process.communicate(timeout=10)
        os.close(master)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    final_state = json.loads(state_path.read_text())
    calls = [json.loads(line) for line in log_path.read_text().splitlines()]
    observed = json.dumps(
        {"calls": calls, "state": final_state, "stdout": result.stdout, "stderr": result.stderr},
        sort_keys=True,
    )
    assert "lin_api_" not in observed.casefold()
    assert all(call[:1] != ["secret"] for call in calls)
    mutation_indices = [index for index, call in enumerate(calls) if "--method" in call]
    for mutation_index in mutation_indices:
        repository_reads = [
            index for index, call in enumerate(calls[:mutation_index]) if REPOSITORY_JQ in call
        ]
        assert len(repository_reads) == 2
        assert confirmation == "Guardantix/doc-lattice"
        assert "Type Guardantix/doc-lattice to apply:" in result.stderr
    if mutation_indices and "environment policy verified" in result.stdout:
        last_mutation = mutation_indices[-1]
        readback_calls = [
            call
            for call in calls[last_mutation + 1 :]
            if call[:3] == ["api", "--hostname", "github.com"] and call[3] != "--method"
        ]
        assert any(call[3].endswith("/deployment-branch-policies") for call in readback_calls)
        if "gh secret set" in result.stdout:
            assert result.stdout.index("environment policy verified") < result.stdout.index(
                "gh secret set"
            )
    return result, final_state, calls


def test_rendered_bootstrap_is_the_third_managed_artifact_and_is_valid_bash(
    tmp_path: Path,
):
    """Render workflows followed by a dependency-minimal Bash bootstrap."""
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")

    assert len(artifacts) == 3
    bootstrap = artifacts[2]
    assert bootstrap.relative_path == BOOTSTRAP_PATH

    script_path = tmp_path / "doc-lattice-bootstrap.sh"
    script_path.write_text(bootstrap.text)
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-n", str(script_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "lin_api_" not in bootstrap.text
    assert "curl" not in bootstrap.text
    assert "\njq " not in bootstrap.text
    assert " --jq " in bootstrap.text
    assert "python" not in bootstrap.text.lower()
    assert bootstrap.text.splitlines()[:5] == [
        "#!/usr/bin/env bash",
        "# doc-lattice-managed: github-ci-v1",
        "# doc-lattice-artifact: bootstrap",
        "# doc-lattice-version: 2.1.0",
        "# doc-lattice-repository: Guardantix/doc-lattice",
    ]
    assert "--force" not in bootstrap.text
    assert "--yes" not in bootstrap.text
    assert " -y" not in bootstrap.text
    assert 'return "$EXIT_FINDING"' in bootstrap.text


@pytest.mark.parametrize(
    ("repository", "version"),
    [("not-a-repository", "2.1.0"), ("Guardantix/doc-lattice", "2.1.0rc1")],
)
def test_render_bootstrap_validates_identity_and_final_release(repository: str, version: str):
    """Reject invalid bootstrap identities and non-final release pins."""
    with pytest.raises(ConfigError):
        render_bootstrap(repository, version)


def test_render_bootstrap_is_deterministic_and_collision_safe():
    """Render token-like repository text once without recursive substitution."""
    first = render_bootstrap("a/__VERSION__", "2.1.0")
    second = render_bootstrap("a/__VERSION__", "2.1.0")

    assert first == second
    assert "EXPECTED_REPOSITORY='a/__VERSION__'" in first.text


@pytest.mark.parametrize("operation", ["plan", "verify"])
def test_read_only_operations_report_exact_installation(tmp_path: Path, operation: str):
    """Return success only for exact observable remote metadata."""
    result, _, _ = _run_bootstrap(tmp_path, _state(), operation)

    assert result.returncode == 0, result.stderr
    assert "policy: exact" in result.stdout
    assert "environment secret DOC_LATTICE_LINEAR_API_KEY: present" in result.stdout


@pytest.mark.parametrize(
    ("state", "expected_output"),
    [
        (_state(environment=_exact_environment(secret_present=False)), "environment secret"),
        (_state(repo_secrets=["LINEAR_API_KEY"]), "LINEAR_API_KEY"),
    ],
)
def test_plan_reports_observable_manual_work(tmp_path: Path, state, expected_output: str):
    """Return a finding for absent dedicated metadata or broader repository metadata."""
    result, _, _ = _run_bootstrap(tmp_path, state)

    assert result.returncode == 1, result.stderr
    assert expected_output in result.stdout


def _mutation_calls(calls):
    """Select state-changing API calls from a fake-gh log."""
    return [call for call in calls if "--method" in call]


def test_apply_creates_absent_environment_after_tty_confirmation(tmp_path: Path):
    """Create custom policy mode then exact-main policy and require later secret work."""
    result, final_state, calls = _run_bootstrap(
        tmp_path,
        _state(environment=None),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 1, result.stderr
    assert _mutation_calls(calls) == [
        [
            "api",
            "--hostname",
            "github.com",
            "--method",
            "PUT",
            "repos/Guardantix/doc-lattice/environments/doc-lattice-linear",
            "--field",
            "deployment_branch_policy[protected_branches]=false",
            "--field",
            "deployment_branch_policy[custom_branch_policies]=true",
        ],
        [
            "api",
            "--hostname",
            "github.com",
            "--method",
            "POST",
            (
                "repos/Guardantix/doc-lattice/environments/doc-lattice-linear/"
                "deployment-branch-policies"
            ),
            "--field",
            "name=main",
            "--field",
            "type=branch",
        ],
    ]
    assert final_state["environment"]["policies"] == [{"name": "main", "type": "branch"}]
    verified = result.stdout.index("environment policy verified")
    secret_command = result.stdout.index(
        "gh secret set DOC_LATTICE_LINEAR_API_KEY --env doc-lattice-linear "
        "--repo Guardantix/doc-lattice"
    )
    assert verified < secret_command
    assert "$CANONICAL_REPOSITORY" not in result.stdout
    mutation_indices = [index for index, call in enumerate(calls) if "--method" in call]
    first_mutation = mutation_indices[0]
    last_mutation = mutation_indices[-1]
    repository_inspections = [index for index, call in enumerate(calls) if REPOSITORY_JQ in call]
    assert len([index for index in repository_inspections if index < first_mutation]) == 2
    readback_endpoints = [
        call[3]
        for call in calls[last_mutation + 1 :]
        if call[:3] == ["api", "--hostname", "github.com"] and call[3] != "--method"
    ]
    assert readback_endpoints == [
        "repos/Guardantix/doc-lattice/actions/secrets",
        "repos/Guardantix/doc-lattice/environments",
        "repos/Guardantix/doc-lattice/environments/doc-lattice-linear",
        ("repos/Guardantix/doc-lattice/environments/doc-lattice-linear/deployment-branch-policies"),
        "repos/Guardantix/doc-lattice/environments/doc-lattice-linear/secrets",
    ]
    assert all(call[:1] != ["secret"] for call in calls)


@pytest.mark.parametrize(
    ("confirmation", "diagnostic"),
    [
        (None, "apply requires an interactive TTY on stdin"),
        ("guardantix/doc-lattice", "repository confirmation did not match"),
    ],
)
def test_apply_requires_tty_exact_canonical_confirmation(
    tmp_path: Path,
    confirmation: str | None,
    diagnostic: str,
):
    """Refuse non-TTY input and wrong-case confirmation before any mutation."""
    result, final_state, calls = _run_bootstrap(
        tmp_path,
        _state(environment=None),
        "apply",
        confirmation=confirmation,
    )

    assert result.returncode == 2
    assert diagnostic in result.stderr
    assert _mutation_calls(calls) == []
    assert final_state["environment"] is None


def test_requested_identity_accepts_case_only_before_exact_canonical_check(tmp_path: Path):
    """Permit a case-insensitive request but reject canonical casing drift."""
    accepted, _, _ = _run_bootstrap(
        tmp_path,
        _state(),
        repository="guardantix/DOC-LATTICE",
    )
    assert accepted.returncode == 0, accepted.stderr

    mismatch_state = _state()
    mismatch_state["repository"] = {
        **mismatch_state["repository"],
        "full_name": "guardantix/doc-lattice",
    }
    mismatch, _, calls = _run_bootstrap(
        tmp_path / "mismatch",
        mismatch_state,
        repository="guardantix/doc-lattice",
    )

    assert mismatch.returncode == 2
    assert "canonical repository identity" in mismatch.stderr
    assert _mutation_calls(calls) == []


def test_default_branch_must_be_exact_main(tmp_path: Path):
    """Reject a repository whose canonical default branch is not main."""
    state = _state()
    state["repository"] = {**state["repository"], "default_branch": "trunk"}

    result, _, calls = _run_bootstrap(tmp_path, state)

    assert result.returncode == 2
    assert "default branch must be exactly main" in result.stderr
    assert all("actions/secrets" not in " ".join(call) for call in calls)


def test_public_repository_does_not_require_plan_metadata(tmp_path: Path):
    """Treat public repositories as eligible without a plan API request."""
    result, _, calls = _run_bootstrap(tmp_path, _state(plan_error=True))

    assert result.returncode == 0, result.stderr
    assert all("users/" not in " ".join(call) and "orgs/" not in " ".join(call) for call in calls)


@pytest.mark.parametrize(
    ("owner_type", "visibility", "plan"),
    [
        ("User", "private", "pro"),
        ("Organization", "private", "team"),
        ("Organization", "private", "enterprise"),
        ("Organization", "internal", "business"),
        ("Organization", "internal", "business_plus"),
    ],
)
def test_paid_private_and_internal_plans_are_eligible(
    tmp_path: Path,
    owner_type: str,
    visibility: str,
    plan: str,
):
    """Accept only the reviewed personal and organization plan names."""
    state = _state(plan=plan)
    state["repository"] = {
        **state["repository"],
        "owner_type": owner_type,
        "visibility": visibility,
    }

    result, _, calls = _run_bootstrap(tmp_path, state)

    assert result.returncode == 0, result.stderr
    expected_endpoint = "users/Guardantix" if owner_type == "User" else "orgs/Guardantix"
    assert any(expected_endpoint in call for call in calls)


@pytest.mark.parametrize(
    ("owner_type", "visibility", "plan"),
    [
        ("User", "private", "free"),
        ("User", "private", "ultimate"),
        ("User", "private", "PRO"),
        ("User", "private", ""),
        ("Organization", "private", "free"),
        ("Organization", "private", "TEAM"),
        ("Organization", "internal", "ultimate"),
    ],
)
def test_unrecognized_or_free_plans_are_ineligible(
    tmp_path: Path,
    owner_type: str,
    visibility: str,
    plan: str,
):
    """Fail closed when plan metadata is empty, free, or unrecognized."""
    state = _state(plan=plan)
    state["repository"] = {
        **state["repository"],
        "owner_type": owner_type,
        "visibility": visibility,
    }

    result, _, calls = _run_bootstrap(tmp_path, state)

    assert result.returncode == 2
    assert _mutation_calls(calls) == []


@pytest.mark.parametrize("owner_type", ["User", "Organization"])
def test_unavailable_plan_metadata_is_tool_error(tmp_path: Path, owner_type: str):
    """Fail closed before state inspection when paid-repository plan lookup fails."""
    state = _state(plan_error=True)
    state["repository"] = {
        **state["repository"],
        "owner_type": owner_type,
        "visibility": "private",
    }

    result, _, calls = _run_bootstrap(tmp_path, state)

    assert result.returncode == 2
    assert "plan metadata is unavailable" in result.stderr
    assert all("actions/secrets" not in " ".join(call) for call in calls)


def test_user_owned_internal_repository_is_ineligible_without_plan_call(tmp_path: Path):
    """Reject the unsupported user/internal shape before inspecting plan or state."""
    state = _state()
    state["repository"] = {
        **state["repository"],
        "owner_type": "User",
        "visibility": "internal",
    }

    result, _, calls = _run_bootstrap(tmp_path, state)

    assert result.returncode == 2
    assert "user-owned internal repositories are not eligible" in result.stderr
    assert all("users/" not in " ".join(call) for call in calls)


def test_repository_secret_permission_failure_stops_state_inspection(tmp_path: Path):
    """Make repository secret-name enumeration the first and mandatory state call."""
    result, _, calls = _run_bootstrap(tmp_path, _state(repo_secret_error=True))

    assert result.returncode == 2
    api_calls = [call for call in calls if call[:1] == ["api"]]
    assert "actions/secrets" in " ".join(api_calls[-1])
    assert all("/environments" not in " ".join(call) for call in api_calls)
    assert _mutation_calls(calls) == []


@pytest.mark.parametrize("operation", ["plan", "verify"])
@pytest.mark.parametrize("failed_normalization", [1, 2, 3, 4])
def test_read_only_sort_failure_is_unreliable_state_error(
    tmp_path: Path,
    operation: str,
    failed_normalization: int,
):
    """Fail closed when any metadata-name or policy normalization fails."""
    result, _, calls = _run_bootstrap(
        tmp_path,
        _state(repo_secrets=["UNRELATED_NAME"]),
        operation,
        sort_fail_calls=(failed_normalization,),
    )

    assert result.returncode == 2
    assert "normalization failed" in result.stderr
    assert "state is unreliable" in result.stderr
    assert "policy: exact" not in result.stdout
    assert _mutation_calls(calls) == []


def test_apply_sort_failure_cannot_erase_mismatched_environment(tmp_path: Path):
    """Never turn repeated failed environment normalization into create permission."""
    mismatched = {
        "custom": True,
        "protected": False,
        "policies": [{"name": "release/*", "type": "branch"}],
        "secrets": [],
    }
    result, final_state, calls = _run_bootstrap(
        tmp_path,
        _state(environment=mismatched, repo_secrets=["UNRELATED_NAME"]),
        "apply",
        confirmation="Guardantix/doc-lattice",
        sort_fail_calls=(2, 4),
    )

    assert result.returncode == 2
    assert "environment-name normalization failed" in result.stderr
    assert _mutation_calls(calls) == []
    assert final_state["environment"] == mismatched


def test_case_variant_environment_with_exact_policy_is_existing(tmp_path: Path):
    """Recognize GitHub's case-insensitive environment identity during planning."""
    result, _, calls = _run_bootstrap(
        tmp_path,
        _state(environment_name="Doc-Lattice-Linear"),
    )

    assert result.returncode == 0, result.stderr
    assert "policy: exact" in result.stdout
    assert _mutation_calls(calls) == []


def test_apply_refuses_case_variant_mismatched_environment(tmp_path: Path):
    """Never create over a case-variant environment with unreviewed policy."""
    mismatched = {
        "custom": True,
        "protected": False,
        "policies": [{"name": "release/*", "type": "branch"}],
        "secrets": [],
    }
    result, final_state, calls = _run_bootstrap(
        tmp_path,
        _state(
            environment=mismatched,
            environment_name="Doc-Lattice-Linear",
        ),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 2
    assert "refusing to narrow or take ownership" in result.stderr
    assert _mutation_calls(calls) == []
    assert final_state["environment"] == mismatched
    assert final_state["environment_name"] == "Doc-Lattice-Linear"


def test_environment_name_translation_failure_is_tool_error(tmp_path: Path):
    """Fail closed if case-insensitive environment-name comparison cannot run."""
    result, _, calls = _run_bootstrap(
        tmp_path,
        _state(),
        tr_fail_calls=(3,),
    )

    assert result.returncode == 2
    assert "environment-name comparison failed" in result.stderr
    assert "state is unreliable" in result.stderr
    assert _mutation_calls(calls) == []


def test_exact_apply_rerun_confirms_without_mutation(tmp_path: Path):
    """Require confirmation even when the current observable state is exact."""
    result, _, calls = _run_bootstrap(
        tmp_path,
        _state(),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 0, result.stderr
    assert "Type Guardantix/doc-lattice to apply:" in result.stderr
    assert _mutation_calls(calls) == []
    repository_calls = [call for call in calls if REPOSITORY_JQ in call]
    assert len(repository_calls) == 2
    assert "environment policy verified" in result.stdout


def test_safe_incomplete_environment_only_posts_main_policy(tmp_path: Path):
    """Resume the sole owned partial state without recreating the environment."""
    safe_incomplete = {
        "custom": True,
        "protected": False,
        "policies": [],
        "secrets": [],
    }

    result, final_state, calls = _run_bootstrap(
        tmp_path,
        _state(environment=safe_incomplete),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 1, result.stderr
    mutations = _mutation_calls(calls)
    assert len(mutations) == 1
    assert mutations[0][mutations[0].index("--method") + 1] == "POST"
    assert final_state["environment"]["policies"] == [{"name": "main", "type": "branch"}]


@pytest.mark.parametrize(
    "environment",
    [
        {
            "custom": False,
            "protected": True,
            "policies": [{"name": "main", "type": "branch"}],
            "secrets": [],
        },
        {
            "custom": True,
            "protected": False,
            "policies": [{"name": "release/*", "type": "branch"}],
            "secrets": [],
        },
        {
            "custom": True,
            "protected": False,
            "policies": [],
            "secrets": ["DOC_LATTICE_LINEAR_API_KEY"],
        },
    ],
)
def test_apply_refuses_ambiguous_existing_policy(tmp_path: Path, environment):
    """Never narrow or assume ownership of a mismatched existing environment."""
    result, _, calls = _run_bootstrap(
        tmp_path,
        _state(environment=environment),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 2
    assert "refusing to narrow or take ownership" in result.stderr
    assert _mutation_calls(calls) == []


def test_apply_rejects_state_change_during_confirmation(tmp_path: Path):
    """Abort when the reinspection fingerprint differs from the reviewed state."""
    changed = _exact_environment(secret_present=False)
    result, _, calls = _run_bootstrap(
        tmp_path,
        _state(change_on_reinspection=changed),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 2
    assert "state changed during confirmation" in result.stderr
    assert "fresh plan then apply" in result.stderr
    assert _mutation_calls(calls) == []


def test_apply_fingerprints_raw_reviewed_plan_metadata(tmp_path: Path):
    """Reject a paid-plan metadata change during confirmation."""
    state = _state(plan="team", plan_on_reinspection="enterprise")
    state["repository"] = {
        **state["repository"],
        "visibility": "private",
    }

    result, _, calls = _run_bootstrap(
        tmp_path,
        state,
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 2
    assert "state changed during confirmation" in result.stderr
    assert _mutation_calls(calls) == []


def test_apply_rejects_canonical_change_during_reinspection(tmp_path: Path):
    """Request a fresh plan/apply if canonical identity changes while confirming."""
    result, _, calls = _run_bootstrap(
        tmp_path,
        _state(canonical_on_reinspection="guardantix/doc-lattice"),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 2
    assert "canonical repository identity" in result.stderr
    assert "fresh plan then apply" in result.stderr
    assert result.stderr.count("run a fresh plan then apply") == 1
    assert _mutation_calls(calls) == []


def test_apply_default_branch_drift_requests_fresh_plan(tmp_path: Path):
    """Add fresh-plan guidance to a default-branch failure during reinspection."""
    result, _, calls = _run_bootstrap(
        tmp_path,
        _state(default_branch_on_reinspection="trunk"),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 2
    assert "default branch must be exactly main" in result.stderr
    assert "run a fresh plan then apply" in result.stderr
    assert result.stderr.count("run a fresh plan then apply") == 1
    assert _mutation_calls(calls) == []


def test_apply_plan_drift_requests_fresh_plan(tmp_path: Path):
    """Add fresh-plan guidance to an eligibility failure during reinspection."""
    state = _state(plan="team", plan_on_reinspection="free")
    state["repository"] = {**state["repository"], "visibility": "private"}
    result, _, calls = _run_bootstrap(
        tmp_path,
        state,
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 2
    assert "organization plan is not recognized as eligible" in result.stderr
    assert "run a fresh plan then apply" in result.stderr
    assert result.stderr.count("run a fresh plan then apply") == 1
    assert _mutation_calls(calls) == []


def test_put_failure_stops_before_branch_policy_and_reports_progress(tmp_path: Path):
    """Stop after an unconfirmed PUT without POST or rollback."""
    result, final_state, calls = _run_bootstrap(
        tmp_path,
        _state(environment=None, put_error=True),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 2
    assert "environment PUT did not complete observably" in result.stderr
    assert "no branch-policy POST was attempted" in result.stderr
    assert "rerun plan then apply" in result.stderr
    assert [call[call.index("--method") + 1] for call in _mutation_calls(calls)] == ["PUT"]
    assert final_state["environment"] is None


def test_partial_post_failure_preserves_safe_remote_progress(tmp_path: Path):
    """Do not roll back an environment whose policy creation failed after PUT."""
    result, final_state, calls = _run_bootstrap(
        tmp_path,
        _state(environment=None, post_error=True),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 2
    assert "custom policy mode was created" in result.stderr
    assert "main branch-policy POST did not complete observably" in result.stderr
    assert "rerun plan then apply" in result.stderr
    assert [call[call.index("--method") + 1] for call in _mutation_calls(calls)] == [
        "PUT",
        "POST",
    ]
    assert final_state["environment"] == {
        "custom": True,
        "protected": False,
        "policies": [],
        "secrets": [],
    }


def test_safe_incomplete_post_failure_reports_preserved_state(tmp_path: Path):
    """Name the already-reviewed safe state when its sole POST cannot complete."""
    safe_incomplete = {
        "custom": True,
        "protected": False,
        "policies": [],
        "secrets": [],
    }
    result, final_state, calls = _run_bootstrap(
        tmp_path,
        _state(environment=safe_incomplete, post_error=True),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 2
    assert "environment custom policy mode remains present with no reviewed policy" in result.stderr
    assert "main branch-policy POST did not complete observably" in result.stderr
    assert "rerun plan then apply" in result.stderr
    assert [call[call.index("--method") + 1] for call in _mutation_calls(calls)] == ["POST"]
    assert final_state["environment"] == safe_incomplete


@pytest.mark.parametrize("secret_name", ["LINEAR_API_KEY", "DOC_LATTICE_LINEAR_API_KEY"])
def test_apply_prints_broader_secret_deletion_only_after_verification(
    tmp_path: Path,
    secret_name: str,
):
    """Print, but never execute, each repository-scope migration command."""
    state = _state(repo_secrets=[secret_name])

    refused, _, refused_calls = _run_bootstrap(tmp_path / "refused", state, "apply")
    assert refused.returncode == 2
    assert "gh secret delete" not in refused.stdout
    assert _mutation_calls(refused_calls) == []

    result, _, calls = _run_bootstrap(
        tmp_path / "confirmed",
        state,
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 1, result.stderr
    verified = result.stdout.index("environment policy verified")
    delete_command = result.stdout.index(
        f"gh secret delete {secret_name} --repo Guardantix/doc-lattice"
    )
    assert verified < delete_command
    assert "$CANONICAL_REPOSITORY" not in result.stdout
    assert all(call[:1] != ["secret"] for call in calls)


def test_successful_verify_reminds_about_local_audit(tmp_path: Path):
    """Explain the remote verifier's boundary even when metadata is exact."""
    result, _, _ = _run_bootstrap(tmp_path, _state(), "verify")

    assert result.returncode == 0, result.stderr
    assert "run local doc-lattice ci audit" in result.stdout
    assert "cannot prove legacy workflow removal" in result.stdout


def test_incomplete_verify_returns_finding_and_local_audit_reminder(tmp_path: Path):
    """Keep the local-audit boundary visible when remote metadata is incomplete."""
    result, _, _ = _run_bootstrap(
        tmp_path,
        _state(environment=_exact_environment(secret_present=False)),
        "verify",
    )

    assert result.returncode == 1, result.stderr
    assert "run local doc-lattice ci audit" in result.stdout


def test_load_state_uses_reviewed_read_only_api_order(tmp_path: Path):
    """Enumerate secret names before all other environment state metadata."""
    result, _, calls = _run_bootstrap(tmp_path, _state())

    assert result.returncode == 0, result.stderr
    state_endpoints = [call[3] for call in calls if call[:3] == ["api", "--hostname", "github.com"]]
    assert state_endpoints[1:] == [
        "repos/Guardantix/doc-lattice/actions/secrets",
        "repos/Guardantix/doc-lattice/environments",
        "repos/Guardantix/doc-lattice/environments/doc-lattice-linear",
        ("repos/Guardantix/doc-lattice/environments/doc-lattice-linear/deployment-branch-policies"),
        "repos/Guardantix/doc-lattice/environments/doc-lattice-linear/secrets",
    ]


@pytest.mark.parametrize("arguments", [[], ["plan"], ["destroy", "Guardantix/doc-lattice"]])
def test_invalid_invocation_fails_before_auth_or_api(tmp_path: Path, arguments: list[str]):
    """Validate exact argument count and operation before invoking gh."""
    result, _, calls = _run_bootstrap(tmp_path, _state(), arguments=arguments)

    assert result.returncode == 2
    assert calls == []


def test_requested_identity_mismatch_fails_before_auth_or_api(tmp_path: Path):
    """Reject a different requested repository without touching gh."""
    result, _, calls = _run_bootstrap(
        tmp_path,
        _state(),
        repository="Guardantix/other",
    )

    assert result.returncode == 2
    assert calls == []


@pytest.mark.parametrize("repository", ["Guardantix/doc-lattice", "Guardantix/other"])
@pytest.mark.parametrize("failed_calls", [(1,), (2,), (1, 2)])
def test_identity_translation_failure_stops_before_auth_or_api(
    tmp_path: Path,
    repository: str,
    failed_calls: tuple[int, ...],
):
    """Never treat failed ASCII identity normalization as an equal identity."""
    result, _, calls = _run_bootstrap(
        tmp_path,
        _state(),
        repository=repository,
        tr_fail_calls=failed_calls,
    )

    assert result.returncode == 2
    assert "repository identity normalization failed" in result.stderr
    assert calls == []


def test_missing_gh_fails_before_api(tmp_path: Path):
    """Require gh on PATH before attempting authentication or API access."""
    result, _, calls = _run_bootstrap(tmp_path, _state(), include_gh=False)

    assert result.returncode == 2
    assert "gh is required on PATH" in result.stderr
    assert calls == []


def test_auth_failure_fails_before_api(tmp_path: Path):
    """Require github.com authentication before the first API request."""
    result, _, calls = _run_bootstrap(tmp_path, _state(auth_ok=False))

    assert result.returncode == 2
    assert calls == [["auth", "status", "--hostname", "github.com"]]


def test_secret_set_command_is_printed_but_never_invoked(tmp_path: Path):
    """Keep secret value entry as an explicit human action after exact read-back."""
    result, _, calls = _run_bootstrap(
        tmp_path,
        _state(environment=None),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 1, result.stderr
    assert "environment policy verified" in result.stdout
    assert (
        "gh secret set DOC_LATTICE_LINEAR_API_KEY --env doc-lattice-linear "
        "--repo Guardantix/doc-lattice"
    ) in result.stdout
    assert "$CANONICAL_REPOSITORY" not in result.stdout
    assert all(call[:1] != ["secret"] for call in calls)


def test_post_mutation_readback_failure_suppresses_secret_commands(tmp_path: Path):
    """Print no migration command unless the complete target read-back succeeds."""
    result, _, calls = _run_bootstrap(
        tmp_path,
        _state(environment=None, environment_secret_error_after_mutation=True),
        "apply",
        confirmation="Guardantix/doc-lattice",
    )

    assert result.returncode == 2
    last_mutation = max(index for index, call in enumerate(calls) if "--method" in call)
    readback_endpoints = [
        call[3]
        for call in calls[last_mutation + 1 :]
        if call[:3] == ["api", "--hostname", "github.com"] and call[3] != "--method"
    ]
    assert readback_endpoints == [
        "repos/Guardantix/doc-lattice/actions/secrets",
        "repos/Guardantix/doc-lattice/environments",
        "repos/Guardantix/doc-lattice/environments/doc-lattice-linear",
        ("repos/Guardantix/doc-lattice/environments/doc-lattice-linear/deployment-branch-policies"),
        "repos/Guardantix/doc-lattice/environments/doc-lattice-linear/secrets",
    ]
    assert "environment policy verified" not in result.stdout
    assert "gh secret set" not in result.stdout
    assert "gh secret delete" not in result.stdout
    assert all(call[:1] != ["secret"] for call in calls)
