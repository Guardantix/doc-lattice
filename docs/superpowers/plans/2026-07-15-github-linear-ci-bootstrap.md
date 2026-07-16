# GitHub Linear CI Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit GitHub installation mode that creates safe offline and trusted Linear
workflows, a human-run protected-environment bootstrap, managed refresh, and an offline policy
audit without adding a network client to doc-lattice.

**Architecture:** A new `doc_lattice.github_ci` package owns typed repository identities, pure
artifact rendering, typed workflow parsing, audit rules, and narrowly scoped managed-artifact
filesystem operations. The existing CLI remains the impure shell: `init --github` performs
create-only publication, while a new `ci` group exposes read-only audit and preview plus an
explicitly confirmed refresh. The generated Bash script is the only GitHub administration surface;
it invokes `gh` only when a maintainer runs it.

**Tech Stack:** Python 3.13, Typer, Rich, ruamel.yaml, existing durable persistence primitives,
Bash 3.2, GitHub CLI, pytest, Ruff, ty.

**Approved design:**
[GitHub Linear CI Bootstrap Design](../specs/2026-07-15-github-linear-ci-bootstrap-design.md)

---

## Decisions fixed by this plan

### Bootstrap exit codes

The generated script uses the same 0/1/2 distinction as local gates, with operation-specific
meaning:

| Operation | `0` | `1` | `2` |
|-----------|-----|-----|-----|
| `plan` | Observable installation is exact. | State is readable and eligible, but safe apply work, secret migration, or manual policy remediation remains. | Invocation, authentication, authorization, canonical identity, plan eligibility, or reliable inspection failed. |
| `verify` | Observable installation is exact. | Observable state is incomplete or violates policy, including either broader repository secret. | Eligibility or reliable inspection could not be established. |
| `apply` | Policy is exact and final observable verification passes. | Policy is exact, but the maintainer still needs to set the environment secret or delete a broader repository secret. | Confirmation was unavailable/refused, mutation failed, or existing state could not be safely owned or narrowed. |

`apply` returning `1` after a successful first-time policy setup is expected. It prints the secret
and cleanup commands only after policy read-back succeeds; the maintainer then runs `verify` until
it returns `0`.

### Action identities

Use these exact official tag commits, resolved during planning on 2026-07-15:

- `actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5` (`v4.3.1`)
- `astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e` (`v6.8.0`)

Both generated workflows set checkout `persist-credentials: false`. setup-uv receives
`enable-cache: false`; no other cache action is generated.

### README ordering

README may show the generic `gh secret set` command only inside the numbered post-`apply` step.
The immediately preceding text and shell comment must say to stop unless `apply` printed
`environment policy verified`. No earlier example or troubleshooting section may show that command.

## File structure

### New production files

- `src/doc_lattice/github_ci/__init__.py`: package boundary only, with no eager I/O.
- `src/doc_lattice/github_ci/model.py`: immutable identities, artifact state, parsed workflow, and
  audit finding models.
- `src/doc_lattice/github_ci/identity.py`: final-release validation and case-insensitive GitHub.com
  repository/origin parsing.
- `src/doc_lattice/github_ci/render.py`: deterministic ownership markers and all three generated
  artifacts, including the pinned action identities.
- `src/doc_lattice/github_ci/workflow_parser.py`: the ruamel.yaml untyped-to-typed boundary.
- `src/doc_lattice/github_ci/audit.py`: filesystem-free global and managed workflow policy rules.
- `src/doc_lattice/github_ci/filesystem.py`: contained discovery, preflight, diff, create, and
  atomic managed replacement.
- `src/doc_lattice/cli/commands/ci.py`: Typer `ci audit` and `ci refresh` adapters plus TTY
  confirmation.

### New tests

- `tests/test_github_ci_identity.py`
- `tests/test_github_ci_render.py`
- `tests/test_github_ci_bootstrap.py`
- `tests/test_github_ci_workflow_parser.py`
- `tests/test_github_ci_audit.py`
- `tests/test_github_ci_filesystem.py`
- `tests/cli/test_ci.py`

### Modified files

- `src/doc_lattice/cli/commands/init.py`: add `--github` and `--repository`, preflight all managed
  paths, and publish missing artifacts create-only.
- `src/doc_lattice/cli/application.py`: register the `ci` command group.
- `tests/cli/test_init.py`: cover GitHub-mode contracts without changing ordinary init behavior.
- `tests/cli/test_contract.py`: cover help and the commands that deliberately do not accept
  `--config`.
- `tests/test_package_metadata.py`: pin the user-documentation migration and ordering contract.
- `README.md`: own installation, migration, command, exit-code, and risk documentation.
- `ARCHITECTURE.md`: record the reviewed-script administration boundary and new package ownership.
- `CHANGELOG.md`: record the new installation and audit surface under Unreleased.

## Task 1: Add typed models and repository identity validation

**Files:**

- Create: `src/doc_lattice/github_ci/__init__.py`
- Create: `src/doc_lattice/github_ci/model.py`
- Create: `src/doc_lattice/github_ci/identity.py`
- Create: `tests/test_github_ci_identity.py`

- [ ] **Step 1: Write failing identity and release-version tests**

Create `tests/test_github_ci_identity.py` with table-driven coverage for explicit names and every
supported origin form:

```python
"""Tests for GitHub repository identity and generator-version validation."""

import pytest

from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci.identity import (
    parse_origin_repository,
    parse_repository,
    validate_final_release_version,
)


@pytest.mark.parametrize(
    "value",
    [
        "Guardantix/doc-lattice",
        "guardantix/DOC-LATTICE",
        "a/a",
        "owner/repo.name_with-punctuation",
    ],
)
def test_parse_repository_accepts_one_ascii_owner_and_repo_segment(value: str):
    identity = parse_repository(value)
    assert identity.display == value
    assert identity.comparison_key == value.lower()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:Guardantix/doc-lattice.git", "Guardantix/doc-lattice"),
        ("git@GITHUB.COM:Guardantix/doc-lattice", "Guardantix/doc-lattice"),
        ("ssh://git@github.com/Guardantix/doc-lattice.git", "Guardantix/doc-lattice"),
        ("https://github.com/Guardantix/doc-lattice", "Guardantix/doc-lattice"),
        ("https://GITHUB.COM/Guardantix/doc-lattice.git", "Guardantix/doc-lattice"),
    ],
)
def test_parse_origin_repository_accepts_documented_forms(url: str, expected: str):
    assert parse_origin_repository(url).display == expected


@pytest.mark.parametrize(
    "value",
    [
        "owner",
        "owner/repo/extra",
        "owner/repo?ref=main",
        "owner/repo#fragment",
        "owner/repo name",
        "owner/repo\nname",
        "-owner/repo",
        "owner-/repo",
    ],
)
def test_parse_repository_rejects_ambiguous_or_unsafe_values(value: str):
    with pytest.raises(ConfigError):
        parse_repository(value)


@pytest.mark.parametrize(
    "url",
    [
        "git@gitlab.com:Guardantix/doc-lattice.git",
        "git://github.com/Guardantix/doc-lattice.git",
        "http://github.com/Guardantix/doc-lattice.git",
        "ssh://root@github.com/Guardantix/doc-lattice.git",
        "https://user@github.com/Guardantix/doc-lattice.git",
        "https://github.com/Guardantix/doc-lattice/extra",
        "https://github.com/Guardantix/doc-lattice.git?ref=main",
        "https://github.com/Guardantix/doc-lattice.git#fragment",
    ],
)
def test_parse_origin_repository_rejects_unsupported_forms(url: str):
    with pytest.raises(ConfigError):
        parse_origin_repository(url)


@pytest.mark.parametrize("version", ["0.1.0", "2.0.0", "10.24.301"])
def test_validate_final_release_version_accepts_three_numeric_components(version: str):
    assert validate_final_release_version(version) == tuple(int(part) for part in version.split("."))


@pytest.mark.parametrize(
    "version",
    ["2.0", "2.0.0.dev1", "2.1.0rc1", "2.0.0+local", "v2.0.0", "latest"],
)
def test_validate_final_release_version_rejects_nonfinal_pins(version: str):
    with pytest.raises(ConfigError, match="final release version"):
        validate_final_release_version(version)
```

- [ ] **Step 2: Run the identity test to verify it fails**

Run:

```bash
uv run --group dev pytest tests/test_github_ci_identity.py -v
```

Expected: collection fails because `doc_lattice.github_ci` does not exist.

- [ ] **Step 3: Add the immutable shared models**

Create an empty-docstring package initializer and `model.py` with the exact fields later tasks use:

```python
"""Typed models shared by GitHub CI rendering, audit, and filesystem adapters."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

ArtifactRole = Literal["offline", "linear", "bootstrap"]
ArtifactAction = Literal["current", "create", "replace"]
TriggerShape = Literal["null", "mapping", "sequence"]
PermissionValue = str | tuple[tuple[str, str], ...] | None


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """Validated GitHub.com owner/repository identity."""

    display: str
    comparison_key: str


@dataclass(frozen=True, slots=True)
class ManagedArtifact:
    """One canonical generated artifact."""

    role: ArtifactRole
    relative_path: PurePosixPath
    text: str


@dataclass(frozen=True, slots=True)
class ArtifactChange:
    """Preflight result for one canonical artifact path."""

    artifact: ManagedArtifact
    root: Path
    destination: Path
    action: ArtifactAction
    before: bytes | None


@dataclass(frozen=True, slots=True)
class WorkflowTrigger:
    """One normalized GitHub Actions event trigger."""

    name: str
    shape: TriggerShape
    branches: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class WorkflowScalar:
    """One scalar string with its structural path."""

    path: tuple[str, ...]
    value: str


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    """Typed subset of a workflow step used by audit rules."""

    index: int
    step_id: str | None
    name: str | None
    uses: str | None
    run: str | None
    env: tuple[tuple[str, str], ...]
    with_values: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class WorkflowJob:
    """Typed subset of one workflow job used by audit rules."""

    job_id: str
    if_condition: str | None
    environment: str | None
    runs_on: str | None
    permissions: PermissionValue
    env: tuple[tuple[str, str], ...]
    steps: tuple[WorkflowStep, ...]


@dataclass(frozen=True, slots=True)
class WorkflowDocument:
    """Validated audit view of one GitHub Actions workflow."""

    path: Path
    triggers: tuple[WorkflowTrigger, ...]
    permissions: PermissionValue
    jobs: tuple[WorkflowJob, ...]
    scalars: tuple[WorkflowScalar, ...]


@dataclass(frozen=True, slots=True, order=True)
class AuditFinding:
    """Stable local policy finding."""

    path: str
    code: str
    message: str
```

Create `src/doc_lattice/github_ci/__init__.py` as:

```python
"""Safe GitHub Actions scaffolding and local policy audit support."""
```

- [ ] **Step 4: Implement explicit repository and origin parsing**

Create `identity.py` with ASCII-only validation and no remote inference:

```python
"""GitHub.com identity and final-release pin validation."""

import re
from urllib.parse import urlsplit

from ..error_types import ConfigError
from .model import RepositoryIdentity

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
_REPOSITORY = r"[A-Za-z0-9_.-]+"
_EXPLICIT_RE = re.compile(rf"(?P<owner>{_OWNER})/(?P<repo>{_REPOSITORY})")
_SCP_RE = re.compile(
    rf"git@github\.com:(?P<identity>{_OWNER}/{_REPOSITORY})(?:\.git)?",
    re.IGNORECASE,
)
_FINAL_RELEASE_RE = re.compile(r"(?P<major>0|[1-9][0-9]*)\."
                               r"(?P<minor>0|[1-9][0-9]*)\."
                               r"(?P<patch>0|[1-9][0-9]*)")


def parse_repository(value: str) -> RepositoryIdentity:
    """Validate one explicit GitHub owner/repository identity."""
    if _EXPLICIT_RE.fullmatch(value) is None:
        raise ConfigError(
            f"repository {value!r} must be one ASCII GitHub OWNER/REPO identity"
        )
    owner, repository = value.split("/", 1)
    if repository in {".", ".."}:
        raise ConfigError(f"repository segment {repository!r} is ambiguous")
    return RepositoryIdentity(display=value, comparison_key=f"{owner}/{repository}".lower())


def _strip_one_git_suffix(value: str) -> str:
    return value[:-4] if value.lower().endswith(".git") else value


def parse_origin_repository(url: str) -> RepositoryIdentity:
    """Parse one documented GitHub.com SSH or HTTPS origin URL."""
    scp_match = _SCP_RE.fullmatch(url)
    if scp_match is not None:
        return parse_repository(_strip_one_git_suffix(scp_match.group("identity")))

    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"origin URL {url!r} has an invalid port") from exc
    if parsed.query or parsed.fragment or port is not None:
        raise ConfigError(f"origin URL {url!r} has unsupported URL components")
    if parsed.hostname is None or parsed.hostname.lower() != "github.com":
        raise ConfigError(f"origin URL {url!r} is not an unambiguous GitHub.com URL")
    if parsed.scheme == "https":
        if parsed.username is not None or parsed.password is not None:
            raise ConfigError(f"origin URL {url!r} must not contain credentials")
    elif parsed.scheme == "ssh":
        if parsed.username != "git" or parsed.password is not None:
            raise ConfigError(f"origin URL {url!r} must use the git SSH user")
    else:
        raise ConfigError(f"origin URL {url!r} uses unsupported scheme {parsed.scheme!r}")
    identity = _strip_one_git_suffix(parsed.path.removeprefix("/"))
    return parse_repository(identity)


def validate_final_release_version(version: str) -> tuple[int, int, int]:
    """Require the exact three-component final version accepted by GitHub generation."""
    match = _FINAL_RELEASE_RE.fullmatch(version)
    if match is None:
        raise ConfigError(
            f"GitHub artifacts require a final release version such as 2.0.0; got {version!r}"
        )
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )
```

- [ ] **Step 5: Run identity tests and static checks**

Run:

```bash
uv run --group dev pytest tests/test_github_ci_identity.py -v
uv run --group dev ruff check src/doc_lattice/github_ci tests/test_github_ci_identity.py
uv run --group dev ty check src/doc_lattice/github_ci
```

Expected: all identity tests pass and both static checks exit `0`.

- [ ] **Step 6: Commit the identity foundation**

```bash
git add src/doc_lattice/github_ci tests/test_github_ci_identity.py
git commit -m "Add GitHub CI identity models"
```

## Task 2: Render the two pinned workflows

**Files:**

- Create: `src/doc_lattice/github_ci/render.py`
- Create: `tests/test_github_ci_render.py`

- [ ] **Step 1: Write failing workflow-rendering tests**

Create tests that load the generated YAML and also assert byte-level security properties:

```python
"""Tests for deterministic managed GitHub artifact rendering."""

from ruamel.yaml import YAML

from doc_lattice.github_ci.render import (
    BOOTSTRAP_PATH,
    CHECKOUT_REF,
    LINEAR_WORKFLOW_PATH,
    OFFLINE_WORKFLOW_PATH,
    SETUP_UV_REF,
    render_workflows,
)


def _yaml(text: str) -> dict[object, object]:
    return YAML(typ="safe").load(text)


def test_render_workflows_returns_the_two_canonical_paths():
    artifacts = render_workflows("Guardantix/doc-lattice", "2.1.0")
    assert tuple(item.relative_path for item in artifacts) == (
        OFFLINE_WORKFLOW_PATH,
        LINEAR_WORKFLOW_PATH,
    )
    assert BOOTSTRAP_PATH not in {item.relative_path for item in artifacts}


def test_offline_workflow_is_secret_free_and_runs_all_three_gates():
    offline = render_workflows("Guardantix/doc-lattice", "2.1.0")[0].text
    parsed = _yaml(offline)
    assert set(parsed["on"]) == {"push", "pull_request"}
    assert parsed["permissions"] == {"contents": "read"}
    assert "LINEAR_API_KEY" not in offline
    assert "pull_request_target" not in offline
    assert "reconcile" not in offline
    for command in ("ci audit", "check", "lint"):
        assert f"doc-lattice {command}" in offline
    for result in ("rc_audit", "rc_check", "rc_lint"):
        assert f"{result}=$?" in offline


def test_linear_workflow_maps_only_the_environment_secret_on_the_final_step():
    linear = render_workflows("Guardantix/doc-lattice", "2.1.0")[1].text
    parsed = _yaml(linear)
    job = parsed["jobs"]["linear"]
    assert set(parsed["on"]) == {"push", "workflow_dispatch"}
    assert parsed["permissions"] == {"contents": "read"}
    assert job["environment"] == "doc-lattice-linear"
    assert job["if"] == (
        "github.repository == 'Guardantix/doc-lattice' && "
        "github.ref == 'refs/heads/main' && "
        "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
    )
    assert job["steps"][-1]["env"] == {
        "LINEAR_API_KEY": "${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}"
    }
    assert job["steps"][-1]["run"] == (
        '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear --exit-code'
    )
    assert all("env" not in step for step in job["steps"][:-1])
    install = job["steps"][-2]["run"]
    assert "uv python install 3.13" in install
    assert 'uv venv --python 3.13 "$RUNNER_TEMP/doc-lattice-venv"' in install
    assert "uv pip install" in install
    assert "doc-lattice==2.1.0" in install


def test_both_workflows_pin_actions_and_disable_credentials_and_caches():
    for artifact in render_workflows("Guardantix/doc-lattice", "2.1.0"):
        assert f"actions/checkout@{CHECKOUT_REF} # v4.3.1" in artifact.text
        assert f"astral-sh/setup-uv@{SETUP_UV_REF} # v6.8.0" in artifact.text
        assert "persist-credentials: false" in artifact.text
        assert "enable-cache: false" in artifact.text
        assert "actions/cache" not in artifact.text
        assert "doc-lattice==2.1.0" in artifact.text
        assert "# doc-lattice-managed: github-ci-v1" in artifact.text
        assert "# doc-lattice-repository: Guardantix/doc-lattice" in artifact.text
```

- [ ] **Step 2: Run the rendering test to verify it fails**

Run:

```bash
uv run --group dev pytest tests/test_github_ci_render.py -v
```

Expected: collection fails because `doc_lattice.github_ci.render` does not exist.

- [ ] **Step 3: Add deterministic workflow templates and marker rendering**

Create `render.py` with the canonical paths, exact pins, a marker helper, and two static templates.
Use token replacement rather than Python format interpolation so GitHub and shell braces remain
literal:

```python
"""Deterministic rendering for managed GitHub Actions artifacts."""

from pathlib import PurePosixPath

from .identity import parse_repository, validate_final_release_version
from .model import ArtifactRole, ManagedArtifact

OFFLINE_WORKFLOW_PATH = PurePosixPath(".github/workflows/doc-lattice.yml")
LINEAR_WORKFLOW_PATH = PurePosixPath(".github/workflows/doc-lattice-linear.yml")
BOOTSTRAP_PATH = PurePosixPath(".github/doc-lattice-bootstrap.sh")
CHECKOUT_REF = "34e114876b0b11c390a56381ad16ebd13914f8d5"  # pragma: allowlist secret
SETUP_UV_REF = "d0cc045d04ccac9d8b7881df0226f9e82c39688e"  # pragma: allowlist secret


def ownership_header(role: ArtifactRole, repository: str, version: str) -> str:
    """Render the four-line managed ownership marker."""
    return (
        "# doc-lattice-managed: github-ci-v1\n"
        f"# doc-lattice-artifact: {role}\n"
        f"# doc-lattice-version: {version}\n"
        f"# doc-lattice-repository: {repository}\n"
    )


def _replace_tokens(template: str, repository: str, version: str) -> str:
    return (
        template.replace("__REPOSITORY__", repository)
        .replace("__VERSION__", version)
        .replace("__CHECKOUT_REF__", CHECKOUT_REF)
        .replace("__SETUP_UV_REF__", SETUP_UV_REF)
    )


_OFFLINE = """name: doc-lattice
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
permissions:
  contents: read
jobs:
  check:
    name: Offline doc-lattice gates
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@__CHECKOUT_REF__ # v4.3.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@__SETUP_UV_REF__ # v6.8.0
        with:
          enable-cache: false
      - name: Audit, check, and lint
        run: |
          set +e
          uvx --python 3.13 --from doc-lattice==__VERSION__ doc-lattice ci audit --repository __REPOSITORY__
          rc_audit=$?
          uvx --python 3.13 --from doc-lattice==__VERSION__ doc-lattice check
          rc_check=$?
          uvx --python 3.13 --from doc-lattice==__VERSION__ doc-lattice lint
          rc_lint=$?
          [ "$rc_audit" -eq 0 ] && [ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ]
"""

_LINEAR = """name: doc-lattice Linear
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
jobs:
  linear:
    name: Trusted Linear gate
    if: >-
      github.repository == '__REPOSITORY__' &&
      github.ref == 'refs/heads/main' &&
      (github.event_name == 'push' || github.event_name == 'workflow_dispatch')
    environment: doc-lattice-linear
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@__CHECKOUT_REF__ # v4.3.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@__SETUP_UV_REF__ # v6.8.0
        with:
          enable-cache: false
      - name: Install pinned doc-lattice without the Linear secret
        run: |
          uv python install 3.13
          uv venv --python 3.13 "$RUNNER_TEMP/doc-lattice-venv"
          uv pip install --python "$RUNNER_TEMP/doc-lattice-venv/bin/python" doc-lattice==__VERSION__
      - name: Run trusted Linear gate
        env:
          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}
        run: '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear --exit-code'
"""


def render_workflows(repository: str, version: str) -> tuple[ManagedArtifact, ManagedArtifact]:
    """Render the canonical offline and Linear workflows."""
    identity = parse_repository(repository)
    validate_final_release_version(version)
    offline = ownership_header("offline", identity.display, version) + _replace_tokens(
        _OFFLINE, identity.display, version
    )
    linear = ownership_header("linear", identity.display, version) + _replace_tokens(
        _LINEAR, identity.display, version
    )
    return (
        ManagedArtifact("offline", OFFLINE_WORKFLOW_PATH, offline),
        ManagedArtifact("linear", LINEAR_WORKFLOW_PATH, linear),
    )
```

- [ ] **Step 4: Run rendering tests and inspect parsed output**

Run:

```bash
uv run --group dev pytest tests/test_github_ci_render.py -v
uv run --group dev ruff check src/doc_lattice/github_ci/render.py tests/test_github_ci_render.py
uv run --group dev ty check src/doc_lattice/github_ci
```

Expected: tests pass; YAML parses; Ruff and ty exit `0`.

- [ ] **Step 5: Commit the workflow renderer**

```bash
git add src/doc_lattice/github_ci/render.py tests/test_github_ci_render.py
git commit -m "Render pinned GitHub CI workflows"
```

## Task 3: Generate and exercise the human-run bootstrap script

**Files:**

- Modify: `src/doc_lattice/github_ci/render.py`
- Create: `tests/test_github_ci_bootstrap.py`

- [ ] **Step 1: Write the fake-gh harness and failing syntax test**

Create a fake `gh` executable in each test's temporary `bin` directory. The executable must read
`FAKE_GH_STATE`, append every argument vector to the JSON-lines file named by `FAKE_GH_LOG`, and
implement these exact endpoints:

| Method and endpoint | Synthetic response or mutation |
|---------------------|--------------------------------|
| `GET repos/{owner}/{repo}` | `full_name`, `default_branch`, `visibility`, and `owner.type` from state |
| `GET users/{owner}` or `GET orgs/{owner}` | `plan.name` from state, or configured permission failure |
| `GET repos/{owner}/{repo}/actions/secrets` | Repository secret name metadata |
| `GET repos/{owner}/{repo}/environments` | Existing environment names |
| `GET repos/{owner}/{repo}/environments/doc-lattice-linear` | Custom-policy booleans |
| `GET repos/{owner}/{repo}/environments/doc-lattice-linear/deployment-branch-policies` | Policy name/type rows |
| `GET repos/{owner}/{repo}/environments/doc-lattice-linear/secrets` | Environment secret name metadata |
| `PUT repos/{owner}/{repo}/environments/doc-lattice-linear` | Create the selected-policy environment |
| `POST repos/{owner}/{repo}/environments/doc-lattice-linear/deployment-branch-policies` | Add the exact `main` branch rule |

The harness must honor the script's `--jq` selections and `--paginate` flag, fail on an unexpected
argument vector, and never place a secret value in state. Begin with this syntax and artifact-set
test:

```python
"""Behavior tests for the generated human-run GitHub bootstrap."""

import subprocess
from pathlib import Path

from doc_lattice.github_ci.render import BOOTSTRAP_PATH, render_managed_artifacts


def test_rendered_bootstrap_is_the_third_managed_artifact_and_is_valid_bash(tmp_path: Path):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    assert artifacts[-1].relative_path == BOOTSTRAP_PATH
    script = tmp_path / "bootstrap.sh"
    script.write_text(artifacts[-1].text, encoding="utf-8")

    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "lin_api_" not in artifacts[-1].text
    assert "curl" not in artifacts[-1].text
    assert "\njq " not in artifacts[-1].text
    assert " --jq " in artifacts[-1].text
    assert "python" not in artifacts[-1].text.lower()
```

- [ ] **Step 2: Run the bootstrap test to verify it fails**

Run:

```bash
uv run --group dev pytest tests/test_github_ci_bootstrap.py -v
```

Expected: collection fails because `render_managed_artifacts` does not exist.

- [ ] **Step 3: Add the bootstrap template and complete artifact renderer**

Extend `render.py` with `_BOOTSTRAP`, `render_bootstrap`, and the public three-artifact function:

```python
def render_bootstrap(repository: str, version: str) -> ManagedArtifact:
    """Render the reviewed Bash 3.2 GitHub administration script."""
    identity = parse_repository(repository)
    validate_final_release_version(version)
    body = _replace_tokens(_BOOTSTRAP, identity.display, version)
    text = "#!/usr/bin/env bash\n" + ownership_header(
        "bootstrap", identity.display, version
    ) + body
    return ManagedArtifact("bootstrap", BOOTSTRAP_PATH, text)


def render_managed_artifacts(
    repository: str, version: str
) -> tuple[ManagedArtifact, ManagedArtifact, ManagedArtifact]:
    """Render all canonical GitHub installation artifacts."""
    workflows = render_workflows(repository, version)
    return (*workflows, render_bootstrap(repository, version))
```

The static `_BOOTSTRAP` string must use only Bash 3.2 features and implement this exact function
surface:

```bash
set -u

EXPECTED_REPOSITORY='__REPOSITORY__'
ENVIRONMENT='doc-lattice-linear'
EXIT_FINDING=1
EXIT_TOOL_ERROR=2

die() {
  printf 'error: %s\n' "$1" >&2
  exit "$EXIT_TOOL_ERROR"
}

lower_ascii() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

api() {
  gh api --hostname github.com "$@"
}

confirm_repository() {
  [ -t 0 ] || die "apply requires an interactive TTY on stdin"
  printf 'Type %s to apply: ' "$CANONICAL_REPOSITORY" >&2
  IFS= read -r answer || die "confirmation ended before a repository was entered"
  [ "$answer" = "$CANONICAL_REPOSITORY" ] || die "repository confirmation did not match"
}
```

The remaining template functions and their required `gh api` calls are:

1. `load_repository` calls `repos/$REQUESTED_REPOSITORY` with a single `--jq` expression producing
   `full_name`, `default_branch`, `visibility`, and `owner.type`. It reports the canonical
   `full_name`, compares it byte-for-byte with `EXPECTED_REPOSITORY`, requires default branch
   `main`, and exits `2` on a mismatch.
2. `check_eligibility` accepts public repositories immediately. For private/internal repositories,
   it reads `.plan.name` from `users/$OWNER` or `orgs/$OWNER`; personal repositories accept only
   `pro`, while organization repositories accept `team`, `enterprise`, `business`, or
   `business_plus`. An internal repository owned by a user, and empty, `free`, or unrecognized plan
   values, exit `2` before any mutation.
3. `load_state` lists repository secret metadata first and exits `2` if that enumeration fails. It
   then lists environments, reads the target environment when present, lists its branch policies
   and environment secret metadata, and derives these shell values: `ENVIRONMENT_EXISTS`,
   `POLICY_EXACT`, `SAFE_INCOMPLETE`, `ENVIRONMENT_SECRET_PRESENT`, `LEGACY_REPO_SECRET_PRESENT`,
   and `DEDICATED_REPO_SECRET_PRESENT`. It also produces a deterministic `STATE_FINGERPRINT` from
   the non-secret values and sorted metadata names.
4. `POLICY_EXACT=1` only when custom policies are enabled, protected-branch mode is disabled, and
   there is exactly one policy row `main<TAB>branch`. `SAFE_INCOMPLETE=1` only for the same custom
   mode with zero policy rows and no environment secret.
5. `print_state` names the canonical target, eligibility result, policy state, environment-secret
   presence, both broader repository-secret names, and the organization-secret visibility reminder.
   It prints metadata only, never a secret value.
6. `observable_status` returns `0` only for exact policy, present environment secret, and absence of
   both broader repository secrets; otherwise it returns `1`.
7. `apply_policy` performs no mutation when `POLICY_EXACT=1`. For an absent environment, after
   confirmation it sends `PUT` with
   `deployment_branch_policy[protected_branches]=false` and
   `deployment_branch_policy[custom_branch_policies]=true`, then sends `POST` with `name=main` and
   `type=branch`. For `SAFE_INCOMPLETE=1`, it sends only that `POST`. Every other existing mismatch
   exits `2` without mutation.
8. After mutation, `apply_policy` calls `load_state` again and exits `2` unless `POLICY_EXACT=1`.
   Only then does it print `environment policy verified` followed, when needed, by the exact
   `gh secret set DOC_LATTICE_LINEAR_API_KEY --env doc-lattice-linear --repo
   "$CANONICAL_REPOSITORY"` command and repository-scope `gh secret delete` commands for names
   observed in state.
9. `plan` calls the read-only loaders, prints state, and exits with `observable_status`.
10. `verify` calls the same read-only loaders, prints state, and exits with `observable_status`.
    It always reminds the maintainer to run local `doc-lattice ci audit` because remote verification
    cannot prove that the legacy hand-written workflow was removed.
11. `apply` calls the read-only loaders and `print_state`, saves `STATE_FINGERPRINT`, then always
    calls `confirm_repository` before `apply_policy` or printing any secret/migration command. After
    confirmation it repeats repository, eligibility, and state inspection and exits `2` without
    mutation if the fingerprint differs from the reviewed state. It then exits with
    `observable_status`, so a first setup normally exits `1` until the separately entered secret
    and cleanup commands have been completed.

At template entry, require exactly `plan|apply|verify` plus one repository argument. Compare that
argument to the embedded identity with `lower_ascii`, require `gh` in `PATH`, and run
`gh auth status --hostname github.com` before any API call. Invalid operation, argument count,
missing `gh`, authentication failure, or non-TTY/mismatched confirmation exits `2`.
Wrap each mutating API call with an operation-specific diagnostic. A failed environment PUT or
policy POST must name the completed remote state and tell the maintainer to rerun `plan` and then
`apply`; the script never attempts rollback or deletion.

- [ ] **Step 4: Add exit-code and ordering tests against fake gh**

Add parameterized tests for the complete script contract:

```python
def test_plan_and_verify_exit_codes(fake_gh, rendered_script):
    exact = fake_gh.exact_state()
    assert fake_gh.run(rendered_script, "plan", exact).returncode == 0
    assert fake_gh.run(rendered_script, "verify", exact).returncode == 0

    missing_secret = fake_gh.exact_state(environment_secrets=[])
    assert fake_gh.run(rendered_script, "plan", missing_secret).returncode == 1
    assert fake_gh.run(rendered_script, "verify", missing_secret).returncode == 1

    legacy = fake_gh.exact_state(repository_secrets=["LINEAR_API_KEY"])
    result = fake_gh.run(rendered_script, "verify", legacy)
    assert result.returncode == 1
    assert "LINEAR_API_KEY" in result.stdout


def test_apply_verifies_policy_before_printing_secret_command(fake_gh, rendered_script):
    state = fake_gh.absent_environment_state()
    result = fake_gh.run_tty(rendered_script, "apply", state, input_text="Guardantix/doc-lattice\n")

    assert result.returncode == 1
    assert result.stdout.index("environment policy verified") < result.stdout.index(
        "gh secret set DOC_LATTICE_LINEAR_API_KEY"
    )
    assert fake_gh.mutations() == ["PUT environment", "POST main branch policy"]


def test_apply_non_tty_or_wrong_identity_never_mutates(fake_gh, rendered_script):
    state = fake_gh.absent_environment_state()
    non_tty = fake_gh.run(rendered_script, "apply", state, input_text="Guardantix/doc-lattice\n")
    mismatch = fake_gh.run_tty(rendered_script, "apply", state, input_text="wrong/repo\n")
    assert non_tty.returncode == mismatch.returncode == 2
    assert fake_gh.mutations() == []
```

Also cover canonical API casing mismatch, default branch mismatch, GitHub Free private state,
unavailable plan metadata, secret-list permission failure, exact rerun, resumable zero-policy state,
dangerously broad policy, state changed during confirmation, partial POST failure, both broader
secret names, and a successful final verify. Assert every recorded argument and output lacks
`lin_api_`.

- [ ] **Step 5: Run all bootstrap behavior tests**

Run:

```bash
uv run --group dev pytest tests/test_github_ci_render.py tests/test_github_ci_bootstrap.py -v
```

Expected: all renderer, Bash syntax, fake-gh argument, exit-code, and ordering tests pass.

- [ ] **Step 6: Commit the bootstrap renderer**

```bash
git add src/doc_lattice/github_ci/render.py tests/test_github_ci_bootstrap.py
git commit -m "Generate verified GitHub environment bootstrap"
```

## Task 4: Add managed-artifact preflight, diff, create, and refresh operations

**Files:**

- Modify: `src/doc_lattice/github_ci/model.py`
- Create: `src/doc_lattice/github_ci/filesystem.py`
- Create: `tests/test_github_ci_filesystem.py`

- [ ] **Step 1: Write failing create-only and refresh preflight tests**

Build tests around the public functions `preflight_create`, `preflight_refresh`, `render_diff`, and
`apply_changes`:

```python
"""Tests for contained managed GitHub artifact filesystem operations."""

from pathlib import Path

import pytest

from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci.filesystem import (
    apply_changes,
    preflight_create,
    preflight_refresh,
    render_diff,
)
from doc_lattice.github_ci.render import render_managed_artifacts


def test_create_preflight_accepts_only_missing_or_byte_exact_targets(tmp_path: Path):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    changes = preflight_create(tmp_path, artifacts)
    assert [change.action for change in changes] == ["create", "create", "create"]

    for artifact in artifacts:
        destination = tmp_path / artifact.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(artifact.text, encoding="utf-8")
    assert all(change.action == "current" for change in preflight_create(tmp_path, artifacts))


def test_create_preflight_rejects_one_different_target_before_any_write(tmp_path: Path):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    conflict = tmp_path / artifacts[1].relative_path
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"user-owned bytes\r\n")

    with pytest.raises(ConfigError, match=str(artifacts[1].relative_path)):
        preflight_create(tmp_path, artifacts)

    assert not (tmp_path / artifacts[0].relative_path).exists()
    assert conflict.read_bytes() == b"user-owned bytes\r\n"


def test_refresh_accepts_prior_marker_and_rejects_unmarked_or_future_files(tmp_path: Path):
    old = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")
    new = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    for artifact in old:
        destination = tmp_path / artifact.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(artifact.text, encoding="utf-8")
    assert all(change.action == "replace" for change in preflight_refresh(tmp_path, new))

    first = tmp_path / new[0].relative_path
    first.write_text("name: user workflow\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="ownership marker"):
        preflight_refresh(tmp_path, new)
```

Add tests for non-UTF-8, symlinked targets, parent symlinks escaping root, missing artifact creation,
stable unified diff, concurrent create collision, replacement mode preservation, preservation of
exception notes, a replacement target changed after preflight, and a synthetic interruption after
one replacement followed by a successful rerun.

- [ ] **Step 2: Run the filesystem tests to verify they fail**

Run:

```bash
uv run --group dev pytest tests/test_github_ci_filesystem.py -v
```

Expected: collection fails because `doc_lattice.github_ci.filesystem` does not exist.

- [ ] **Step 3: Implement marker parsing and complete-set preflight**

Implement these exact public signatures:

```python
def preflight_create(
    root: Path, artifacts: tuple[ManagedArtifact, ...]
) -> tuple[ArtifactChange, ...]:
    """Accept missing or byte-exact targets and reject every differing existing target."""


def preflight_refresh(
    root: Path, artifacts: tuple[ManagedArtifact, ...]
) -> tuple[ArtifactChange, ...]:
    """Accept current, missing, or valid prior managed targets and reject ambiguous state."""


def render_diff(changes: tuple[ArtifactChange, ...]) -> str:
    """Render stable unified diffs for create and replace actions."""


def apply_changes(changes: tuple[ArtifactChange, ...]) -> None:
    """Publish every preflighted create or atomic replacement in canonical order."""
```

Add the marker model used by both refresh and later audit:

```python
@dataclass(frozen=True, slots=True)
class ManagedMarker:
    """Parsed ownership metadata from a canonical artifact."""

    role: ArtifactRole
    version: str
    repository: RepositoryIdentity
```

Both preflights must resolve each fixed destination with `safe_resolve(destination, root)` before
reading or creating a parent, reject symlinks and non-regular existing files, read exact bytes, and
complete inspection of all three targets before returning. `preflight_refresh` parses the four-line
marker after an optional script shebang, requires the role to agree with the canonical path, and
requires the old repository marker to be a valid identity. It deliberately permits that valid old
identity to differ from the requested identity so refresh is the supported rename, transfer, and
canonical-casing repair path. It accepts generator versions less than or equal to the current
version and rejects newer, malformed, or unmarked files.

Use `difflib.unified_diff` with `a/<path>` and `b/<path>` labels, `/dev/null` for a new file, and
`lineterm=""`; always end nonempty diff output with one newline. `apply_changes` creates contained
parents only after preflight, uses `atomic_create_bytes` for `create`, and
`atomic_replace_bytes` for `replace`. Immediately before each write, re-run
`safe_resolve(destination, change.root)`, reject a changed path type or escaped parent, and require a
replacement destination's bytes still to equal `change.before`. A changed replacement exits `2`
rather than overwriting an edit made after preview; a concurrent creator is rejected by
`atomic_create_bytes`. Wrap `OSError` in `ConfigError`, copy exception notes, name the failed
canonical path, and do not roll back earlier secret-free tracked artifacts.

- [ ] **Step 4: Run filesystem and persistence regression tests**

Run:

```bash
uv run --group dev pytest tests/test_github_ci_filesystem.py tests/test_persistence.py -v
```

Expected: all new and existing persistence tests pass.

- [ ] **Step 5: Commit managed-artifact filesystem support**

```bash
git add src/doc_lattice/github_ci/model.py src/doc_lattice/github_ci/filesystem.py tests/test_github_ci_filesystem.py
git commit -m "Add managed GitHub artifact preflight"
```

## Task 5: Extend init with explicit GitHub generation

**Files:**

- Modify: `src/doc_lattice/cli/commands/init.py:20-107`
- Modify: `tests/cli/test_init.py`

- [ ] **Step 1: Write failing GitHub-mode CLI tests**

Add focused cases without weakening the existing ordinary-init assertions:

```python
def test_init_github_requires_explicit_repository_before_writing(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--github"])
    assert result.exit_code == 2
    assert "--repository is required with --github" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_init_rejects_repository_without_github_mode(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--repository", "Guardantix/doc-lattice"])
    assert result.exit_code == 2
    assert "--repository requires --github" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_init_github_creates_config_and_all_three_managed_artifacts(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["init", "--github", "--repository", "Guardantix/doc-lattice"],
    )
    assert result.exit_code == 0
    assert (tmp_path / ".doc-lattice.yml").exists()
    assert (tmp_path / ".github/workflows/doc-lattice.yml").exists()
    assert (tmp_path / ".github/workflows/doc-lattice-linear.yml").exists()
    assert (tmp_path / ".github/doc-lattice-bootstrap.sh").exists()
    assert "# ===== .github/workflows/doc-lattice.yml" not in result.stdout
    assert "bash .github/doc-lattice-bootstrap.sh plan Guardantix/doc-lattice" in result.stderr


def test_init_github_conflict_preflights_every_artifact_before_config_creation(
    tmp_path: Path, monkeypatch
):
    conflict = tmp_path / ".github/workflows/doc-lattice-linear.yml"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"existing user workflow\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["init", "--github", "--repository", "Guardantix/doc-lattice"],
    )

    assert result.exit_code == 2
    assert not (tmp_path / ".doc-lattice.yml").exists()
    assert not (tmp_path / ".github/workflows/doc-lattice.yml").exists()
    assert conflict.read_bytes() == b"existing user workflow\n"
```

Also test exact reruns, one missing artifact on rerun, development/prerelease/local version rejection
by monkeypatching the command module's `__version__`, concurrent artifact creation, and preservation
of ordinary init's byte-exact stdout/stderr and single config write.

- [ ] **Step 2: Run the init tests to verify the new cases fail**

Run:

```bash
uv run --group dev pytest tests/cli/test_init.py -v
```

Expected: new GitHub-mode tests fail because the options are not registered.

- [ ] **Step 3: Implement preflight-before-write GitHub mode**

Add the options to `init`:

```python
github: Annotated[
    bool,
    typer.Option("--github", help="Create managed GitHub Actions and bootstrap artifacts."),
] = False,
repository: Annotated[
    str | None,
    typer.Option("--repository", help="Exact GitHub OWNER/REPO for generated guards."),
] = None,
```

Inside `exit_on_project_error`, validate the option pairing first. For GitHub mode, call
`parse_repository`, `validate_final_release_version(__version__)`,
`render_managed_artifacts`, and `preflight_create` before creating `.doc-lattice.yml` or a directory.
Then preserve the existing config create-only behavior and call `apply_changes` for missing managed
artifacts. Exact existing artifacts remain untouched. A race reports `ConfigError` with copied
persistence notes and leaves the winner's bytes.

Ordinary init must execute the existing print path byte-for-byte. GitHub mode still prints the
`.gitignore` and pre-commit guidance, but suppresses the legacy pasted workflow because it has just
written the canonical workflow. Its stderr names the three paths, tells the maintainer to review
them, and prints:

```text
bash .github/doc-lattice-bootstrap.sh plan OWNER/REPO
```

- [ ] **Step 4: Run ordinary and GitHub init tests**

Run:

```bash
uv run --group dev pytest tests/cli/test_init.py tests/test_scaffold.py -v
```

Expected: all legacy scaffold and new GitHub-mode tests pass.

- [ ] **Step 5: Commit explicit GitHub init**

```bash
git add src/doc_lattice/cli/commands/init.py tests/cli/test_init.py
git commit -m "Add explicit GitHub artifact initialization"
```

## Task 6: Parse workflow YAML into a typed audit model

**Files:**

- Modify: `src/doc_lattice/github_ci/model.py`
- Create: `src/doc_lattice/github_ci/workflow_parser.py`
- Create: `tests/test_github_ci_workflow_parser.py`

- [ ] **Step 1: Write failing parser-boundary tests**

Cover every GitHub trigger spelling the audit accepts and enough job structure to distinguish the
canonical secret mapping:

```python
"""Tests for the GitHub Actions YAML-to-typed-model boundary."""

from pathlib import Path

import pytest

from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci.workflow_parser import parse_workflow


def test_parse_workflow_normalizes_triggers_jobs_steps_and_scalars(tmp_path: Path):
    path = tmp_path / "workflow.yml"
    text = """name: Example
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
jobs:
  linear:
    if: github.ref == 'refs/heads/main'
    environment: doc-lattice-linear
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@0123456789012345678901234567890123456789
        with:
          persist-credentials: false
      - id: gate
        env:
          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}
        run: doc-lattice linear --exit-code
"""

    workflow = parse_workflow(path, text)

    assert [(trigger.name, trigger.shape, trigger.branches) for trigger in workflow.triggers] == [
        ("push", "mapping", ("main",)),
        ("workflow_dispatch", "null", None),
    ]
    assert workflow.permissions == (("contents", "read"),)
    job = workflow.jobs[0]
    assert job.job_id == "linear"
    assert job.environment == "doc-lattice-linear"
    assert job.steps[0].with_values == (("persist-credentials", "false"),)
    assert job.steps[-1].env == (
        ("LINEAR_API_KEY", "${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}"),
    )
    assert any(scalar.value == "doc-lattice linear --exit-code" for scalar in workflow.scalars)


@pytest.mark.parametrize(
    "on_value",
    [
        "pull_request",
        "[push, pull_request]",
        "{pull_request_target: null}",
        "{schedule: [{cron: '17 3 * * *'}]}",
    ],
)
def test_parse_workflow_accepts_supported_event_shorthands(tmp_path: Path, on_value: str):
    workflow = parse_workflow(
        tmp_path / "workflow.yml",
        f"on: {on_value}\njobs: {{}}\n",
    )
    assert workflow.triggers


@pytest.mark.parametrize(
    "text",
    [
        "on: [push\n",
        "on: push\non: pull_request\njobs: {}\n",
        "- not\n- a\n- mapping\n",
        "on: {push: 3}\njobs: {}\n",
        "on: push\njobs: []\n",
    ],
)
def test_parse_workflow_rejects_yaml_it_cannot_inspect_reliably(tmp_path: Path, text: str):
    with pytest.raises(ConfigError, match="workflow.yml"):
        parse_workflow(tmp_path / "workflow.yml", text)
```

- [ ] **Step 2: Run parser tests to verify they fail**

Run:

```bash
uv run --group dev pytest tests/test_github_ci_workflow_parser.py -v
```

Expected: collection fails because the workflow parser does not exist.

- [ ] **Step 3: Implement the only new untyped YAML boundary**

Create `workflow_parser.py`. It may import `typing.Any` because its `_parser` suffix is recognized by
`scripts/check_typing_boundaries.py`; no other new module may do so. Configure
`YAML(typ="safe")` with duplicate keys disabled, catch ruamel YAML errors, and wrap them in
`ConfigError` naming the workflow.

Implement this public API:

```python
def parse_workflow(path: Path, text: str) -> WorkflowDocument:
    """Validate workflow YAML into the typed subset required by audit."""
```

Normalization rules are exact:

- `on: event` and `on: [event, event]` become null-shape triggers.
- Event mappings allow null, mapping, or sequence configuration so legitimate unrelated `schedule`
  workflows remain inspectable. Normalize `branches` from one string or a string list when the
  configuration is a mapping; retain `None` otherwise. Managed workflows later require their exact
  null/mapping shapes.
- Top-level and job `permissions` become either a scalar string, sorted string pairs, or `None`.
- `jobs` must be a mapping. A reusable-workflow job without `steps` is valid and has an empty step
  tuple; when `steps` exists it must be a list of mappings.
- Normalize booleans in `env` and `with` mappings to lowercase `true`/`false`, numbers with `str`,
  and strings unchanged. Reject containers where a scalar is required.
- Retain `job_id`, `if`, string-form `environment`, string-form `runs-on`, job env, step index, step
  id/name, `uses`, `run`, step env, and step `with` values.
- Recursively collect every string value as `WorkflowScalar(path, value)`. Mapping keys and list
  indices form the path; reject non-string mapping keys because audit could not name them reliably.
- Legitimate unrelated job forms that do not matter to global rules remain parseable. A non-string
  environment or runs-on value normalizes to `None`, which makes the canonical managed check fail
  without making every unrelated workflow a tool error.

- [ ] **Step 4: Run parser tests and the typing-boundary gate**

Run:

```bash
uv run --group dev pytest tests/test_github_ci_workflow_parser.py -v
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev ty check src/doc_lattice/github_ci
```

Expected: parser tests pass, typing.Any remains confined to recognized boundaries, and ty exits `0`.

- [ ] **Step 5: Commit the workflow parser**

```bash
git add src/doc_lattice/github_ci/model.py src/doc_lattice/github_ci/workflow_parser.py tests/test_github_ci_workflow_parser.py
git commit -m "Parse GitHub workflows for policy audit"
```

## Task 7: Implement repository-global and managed audit rules

**Files:**

- Modify: `src/doc_lattice/github_ci/model.py`
- Modify: `src/doc_lattice/github_ci/filesystem.py`
- Create: `src/doc_lattice/github_ci/audit.py`
- Create: `tests/test_github_ci_audit.py`

- [ ] **Step 1: Write failing direct-command detector tests**

Start with the documented heuristic boundary:

```python
"""Tests for local GitHub Actions policy audit."""

from pathlib import Path

import pytest

from doc_lattice.github_ci.audit import (
    audit_global_workflows,
    direct_doc_lattice_invocations,
)
from doc_lattice.github_ci.workflow_parser import parse_workflow


@pytest.fixture
def workflow_factory(tmp_path: Path):
    def create(name: str, text: str):
        return parse_workflow(tmp_path / name, text)

    return create


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("doc-lattice linear --exit-code", (("linear", False),)),
        (
            '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear --exit-code',
            (("linear", False),),
        ),
        ("uvx --from doc-lattice==2.1.0 doc-lattice reconcile target", (("reconcile", False),)),
        ("uv run doc-lattice reconcile --all --dry-run", (("reconcile", True),)),
        ("echo doc-lattice linear", ()),
        ("printf '%s' 'doc-lattice reconcile'", ()),
        ("set +e\ndoc-lattice check\ndoc-lattice lint", (("check", False), ("lint", False))),
        ("if doc-lattice linear; then exit 1; fi", (("linear", False),)),
    ],
)
def test_direct_doc_lattice_invocations(script: str, expected: tuple[tuple[str, bool], ...]):
    assert direct_doc_lattice_invocations(script) == expected
```

- [ ] **Step 2: Write failing global-policy tests**

Use small parsed workflows to prove global scope does not reject legitimate unrelated release
permissions:

```python
def test_global_rules_reject_pr_target_secret_refs_and_pr_mutation(workflow_factory):
    documents = (
        workflow_factory(
            "unsafe.yml",
            """on: pull_request_target
jobs:
  unsafe:
    runs-on: ubuntu-latest
    steps:
      - env:
          LINEAR_API_KEY: ${{ secrets.LINEAR_API_KEY }}
        run: |
          doc-lattice linear --exit-code
          doc-lattice reconcile --all
""",
        ),
    )
    codes = {finding.code for finding in audit_global_workflows(documents)}
    assert codes == {
        "PULL_REQUEST_TARGET",
        "LINEAR_SECRET_REFERENCE",
        "PR_LINEAR_INVOCATION",
        "PR_MUTATING_RECONCILE",
    }


def test_global_rules_allow_unrelated_release_permissions_and_persisted_checkout(workflow_factory):
    documents = (
        workflow_factory(
            "release.yml",
            """on:
  push:
    tags: ['v*']
permissions:
  contents: write
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
""",
        ),
    )
    assert audit_global_workflows(documents) == ()
```

Add fixtures for `pull_request`, `pull_request_review`, and `pull_request_review_comment`; multiline
shell, job env, step env, reusable workflow, `workflow_run`, the dedicated and legacy secret names,
dry-run reconcile, and indirection through an arbitrary script. The indirection fixture must be
documented as undetected, matching the design's stated heuristic limit.

- [ ] **Step 3: Write failing managed-installation tests**

Render the canonical artifacts into a temporary repository and assert:

- Exact artifacts produce no findings.
- Absent workflows directory, either workflow, or bootstrap script produces exit-1 findings.
- Stale marker version produces `STALE_GENERATOR` and directs to `ci refresh`.
- An invalid bootstrap marker is a finding, not a YAML parse error.
- Malformed present workflow YAML raises `ConfigError` and therefore maps to exit `2` later.
- Offline workflow trigger, permission, action pin, checkout credential, cache, or command changes
  produce managed findings.
- Linear job-id, repository/ref/event guard, environment, step order, secret scope, or final command
  changes produce managed findings.
- A repository identity compare is ASCII case-insensitive, while a rename/transfer is
  `REPOSITORY_IDENTITY`.
- The current repository's unrelated `.github/workflows/ci.yml` with `contents: write` in its release
  job is not a managed-permissions finding.

- [ ] **Step 4: Implement shell command detection without executing shell**

In `audit.py`, implement:

```python
def direct_doc_lattice_invocations(script: str) -> tuple[tuple[str, bool], ...]:
    """Return direct doc-lattice commands and reconcile dry-run state from shell text."""
```

Remove backslash-newline continuations, replace remaining newlines with command separators, assign
the result to `normalized`, and tokenize with
`shlex.shlex(normalized, posix=True, punctuation_chars=";&|()")`. Split on punctuation-only
tokens. For each simple command, skip leading `if`, `then`, `do`, `!`, and `env`, plus shell
assignments. Recognize `doc-lattice` or a command path whose final component is `doc-lattice`, or
recognize either form as the payload following `uvx` or `uv run`. Do not treat arguments to `echo`
or `printf` as invocations. For `reconcile`, set the boolean only when `--dry-run` is a distinct
token in that same simple command.

Malformed shell quoting does not become a tool error: return no invocation for the malformed
fragment and let the audit limitation diagnostic remain honest.

- [ ] **Step 5: Implement global audit rules**

Add:

```python
PR_EVENTS = frozenset(
    {"pull_request", "pull_request_review", "pull_request_review_comment"}
)
SECRET_NAMES = frozenset({"LINEAR_API_KEY", "DOC_LATTICE_LINEAR_API_KEY"})


def audit_global_workflows(
    documents: tuple[WorkflowDocument, ...],
) -> tuple[AuditFinding, ...]:
    """Apply repository-wide prohibitions to every parsed workflow."""
```

Emit stable sorted findings for any `pull_request_target`; direct `linear` under a PR event; direct
non-dry-run `reconcile` under a PR event; and either secret name in any scalar value or job/step env
key/value except the exact final-step mapping in canonical
`.github/workflows/doc-lattice-linear.yml`, job `linear`, env key `LINEAR_API_KEY`, value
`${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}`. Global rules run even when a managed workflow also has
drift, and findings are deduplicated by `(path, code, message)`.

- [ ] **Step 6: Add contained workflow discovery and managed marker inspection**

Extend `model.py` with immutable `InstalledArtifact` and `WorkflowDiscovery` models:

```python
@dataclass(frozen=True, slots=True)
class InstalledArtifact:
    """Observed canonical artifact and marker outcome."""

    expected: ManagedArtifact
    text: str
    marker: ManagedMarker | None
    marker_error: str | None


@dataclass(frozen=True, slots=True)
class WorkflowDiscovery:
    """Read-only direct workflow directory result."""

    directory_exists: bool
    documents: tuple[WorkflowDocument, ...]
```

Then extend `filesystem.py` with:

```python
def discover_workflows(root: Path) -> WorkflowDiscovery:
    """Read direct .yml/.yaml workflow files inside the contained workflows directory."""


def inspect_installed_artifacts(
    root: Path, expected: tuple[ManagedArtifact, ...]
) -> tuple[InstalledArtifact | None, ...]:
    """Read canonical artifact text and validated markers without mutating."""
```

Discovery is nonrecursive because GitHub recognizes workflow files directly in
`.github/workflows`. An absent directory is a normal discovery result used for an exit-1 finding.
Present unreadable, non-UTF-8, external-symlink, non-regular, or malformed workflow files raise
`ConfigError`. Pass each parser the repository-relative `.github/workflows/<name>` display path so
audit output is stable and never leaks the maintainer's absolute checkout path. Missing canonical
artifacts return `None`; a present bootstrap with a bad marker is an audit finding, while a present
workflow with bad YAML remains a tool error.

- [ ] **Step 7: Implement managed semantic checks and repository identity drift**

Add:

```python
def audit_managed_installation(
    discovery: WorkflowDiscovery,
    installed: tuple[InstalledArtifact | None, ...],
    repository: RepositoryIdentity,
    running_version: str,
) -> tuple[AuditFinding, ...]:
    """Check canonical presence, marker freshness, identity, and managed workflow semantics."""
```

Use the installed marker's repository literal and version to render and parse the semantic expected
workflow. Compare trigger sets and branch filters, workflow permissions, expected job ids, runs-on,
job permission/env/environment/if fields, action identities, step order, `with` values, and exact
run commands. Report focused codes `MANAGED_TRIGGERS`, `MANAGED_PERMISSIONS`, `MANAGED_JOB`,
`MANAGED_ACTION`, `MANAGED_CHECKOUT`, `MANAGED_CACHE`, `MANAGED_COMMAND`, and `MANAGED_SECRET`.

Compare the explicit/origin repository identity with the installed marker case-insensitively. A
different owner/repository is `REPOSITORY_IDENTITY`; casing alone is accepted locally because only
bootstrap API read-back can prove canonical display casing. Marker version different from
`running_version` is `STALE_GENERATOR`. Require the bootstrap artifact and its marker, but never send
it through YAML parsing.

- [ ] **Step 8: Run all parser and audit tests**

Run:

```bash
uv run --group dev pytest tests/test_github_ci_workflow_parser.py tests/test_github_ci_audit.py -v
uv run --group dev ruff check src/doc_lattice/github_ci tests/test_github_ci_audit.py
uv run --group dev ty check src/doc_lattice/github_ci
```

Expected: direct invocation, global scope, managed scope, malformed input, and identity tests all
pass; static checks exit `0`.

- [ ] **Step 9: Commit the audit engine**

```bash
git add src/doc_lattice/github_ci tests/test_github_ci_audit.py
git commit -m "Audit GitHub CI installation policy"
```

## Task 8: Expose `ci audit` and confirmed `ci refresh`

**Files:**

- Create: `src/doc_lattice/cli/commands/ci.py`
- Modify: `src/doc_lattice/cli/application.py:8-60`
- Create: `tests/cli/test_ci.py`
- Modify: `tests/cli/test_contract.py`

- [ ] **Step 1: Write failing `ci audit` CLI tests**

Create `tests/cli/test_ci.py` with exact exit and output behavior:

```python
"""CLI integration tests for managed GitHub CI audit and refresh."""

from pathlib import Path

from doc_lattice import __version__
from doc_lattice.cli import app
from doc_lattice.github_ci.filesystem import apply_changes, preflight_create
from doc_lattice.github_ci.render import render_managed_artifacts

from .helpers import runner


def _install(root: Path, repository: str = "Guardantix/doc-lattice") -> None:
    artifacts = render_managed_artifacts(repository, __version__)
    apply_changes(preflight_create(root, artifacts))


def test_ci_audit_exact_installation_exits_zero(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["ci", "audit", "--repository", "Guardantix/doc-lattice"]
    )
    assert result.exit_code == 0
    assert result.stdout == "doc-lattice ci audit: ok\n"


def test_ci_audit_policy_finding_exits_one(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    unsafe = tmp_path / ".github/workflows/unsafe.yml"
    unsafe.write_text("on: pull_request_target\njobs: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["ci", "audit", "--repository", "Guardantix/doc-lattice"]
    )
    assert result.exit_code == 1
    assert "unsafe.yml: PULL_REQUEST_TARGET:" in result.stdout


def test_ci_audit_malformed_present_yaml_exits_two(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    broken = tmp_path / ".github/workflows/broken.yml"
    broken.write_text("on: [push\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["ci", "audit", "--repository", "Guardantix/doc-lattice"]
    )
    assert result.exit_code == 2
    assert "broken.yml" in result.stderr
    assert "CONFIG_ERROR" in result.stderr
```

Also test absent workflows before adoption exits `1`, omitted `--repository` resolves the three
documented origin formats through fixed `git config --get remote.origin.url`, missing/ambiguous
origin exits `2`, and explicit repository never invokes git.

- [ ] **Step 2: Write failing refresh preview and confirmation tests**

Add cases for current `0`, stale preview `1`, unsafe state `2`, and apply confirmation:

```python
def test_ci_refresh_previews_stale_managed_artifacts_without_writing(tmp_path: Path, monkeypatch):
    old = render_managed_artifacts("Guardantix/doc-lattice", "1.9.0")
    apply_changes(preflight_create(tmp_path, old))
    before = {item.relative_path: (tmp_path / item.relative_path).read_bytes() for item in old}
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app, ["ci", "refresh", "--repository", "Guardantix/doc-lattice"]
    )

    assert result.exit_code == 1
    assert "--- a/.github/workflows/doc-lattice.yml" in result.stdout
    assert all((tmp_path / path).read_bytes() == data for path, data in before.items())


def test_ci_refresh_apply_non_tty_exits_two_without_writing(tmp_path: Path, monkeypatch):
    old = render_managed_artifacts("Guardantix/doc-lattice", "1.9.0")
    apply_changes(preflight_create(tmp_path, old))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["ci", "refresh", "--repository", "Guardantix/doc-lattice", "--apply"],
        input="Guardantix/doc-lattice\n",
    )
    assert result.exit_code == 2
    assert "interactive TTY" in result.stderr
    assert (tmp_path / old[0].relative_path).read_text(encoding="utf-8") == old[0].text
```

Test successful apply by unit-testing the confirmation helper with a `StringIO` subclass whose
`isatty()` returns `True`, then monkeypatching that helper in the CLI integration test. Cover EOF,
exact mismatch, `--yes` and `--force` as unknown options, missing bootstrap recreation, unmarked
refusal, mixed-version rerun, and repository rename refresh.

- [ ] **Step 3: Register a nested Typer group and implement origin resolution**

Create `ci.py` with one `Typer(no_args_is_help=True)` subapplication and register it through:

```python
def register_ci(app: typer.Typer) -> None:
    """Register offline GitHub CI audit and managed refresh commands."""
    ci_app = typer.Typer(no_args_is_help=True)
    app.add_typer(ci_app, name="ci", help="Audit or refresh managed GitHub CI artifacts.")
```

For omitted audit repository, run this fixed local command with no shell and a short timeout:

```python
subprocess.run(  # noqa: S603 - fixed local command, no shell
    [  # noqa: S607 - git is intentionally resolved from the maintainer's PATH
        "git",
        "config",
        "--get",
        "remote.origin.url",
    ],
    cwd=runtime.cwd,
    capture_output=True,
    text=True,
    check=False,
    timeout=5,
)
```

Parse stdout only on return code `0` and reject empty or multiline output. Missing git, timeout,
nonzero exit, or unsupported origin becomes `ConfigError` and exit `2`. An explicit repository
goes directly through `parse_repository` and performs no subprocess call.

- [ ] **Step 4: Implement `ci audit` orchestration and stable output**

Inside `exit_on_project_error`, resolve identity, call `discover_workflows`,
`inspect_installed_artifacts`, `audit_global_workflows`, and `audit_managed_installation`. Merge and
sort unique findings. Write each as:

```text
<repo-relative-path>: <CODE>: <message>
```

Write `doc-lattice ci audit: ok` when none. Exit with `EXIT_FINDING` for any finding and `0`
otherwise. Do not load `.doc-lattice.yml`, a lattice, a cache, or the network.

- [ ] **Step 5: Implement fail-closed TTY confirmation and refresh**

Import `TextIO` from `typing`, `escape` from `rich.markup`, and add a unit-testable helper:

```python
def require_repository_confirmation(
    stream: TextIO, runtime: CliRuntime, repository: str
) -> None:
    """Require exact repository text from an attached stdin TTY."""
    if not stream.isatty():
        raise ConfigError("ci refresh --apply requires an interactive TTY on stdin")
    runtime.stderr.print(f"Type {escape(repository)} to apply managed refresh:", end=" ")
    answer = stream.readline()
    if answer == "":
        raise ConfigError("refresh confirmation ended before a repository was entered")
    if answer.removesuffix("\n") != repository:
        raise ConfigError("refresh confirmation did not match the requested repository")
```

`ci refresh` requires `--repository`, validates the current final version, renders all artifacts,
and calls `preflight_refresh`. With no changes, print a current-state diagnostic and exit `0` without
prompting. With changes, always write the stable diff to stdout. Without `--apply`, exit `1`. With
`--apply`, call the helper with `typer.get_text_stream("stdin")` before any write, then repeat
`preflight_refresh`. Require the repeated changes and diff to match what the maintainer approved;
otherwise exit `2` and ask for a fresh preview. Call `apply_changes` only for the unchanged repeated
plan and exit `0`. Do not register an assent or force option and do not inspect an environment
variable for confirmation.

- [ ] **Step 6: Register `ci` in the application and update CLI contracts**

Import and call `register_ci(application)` before `register_init`. Change the application docstring
from “all seven commands” to wording that does not embed a count. In `tests/cli/test_contract.py`,
assert global help lists `ci`, `ci --help` lists `audit` and `refresh`, and both `init` and `ci`
reject `--config` as an unknown option.

- [ ] **Step 7: Run all CLI tests**

Run:

```bash
uv run --group dev pytest tests/cli/test_ci.py tests/cli/test_init.py tests/cli/test_contract.py -v
```

Expected: audit 0/1/2, refresh 0/1/2, confirmation, help, and legacy CLI contracts pass.

- [ ] **Step 8: Commit the CI command group**

```bash
git add src/doc_lattice/cli/application.py src/doc_lattice/cli/commands/ci.py tests/cli/test_ci.py tests/cli/test_contract.py
git commit -m "Expose GitHub CI audit and refresh commands"
```

## Task 9: Lock the end-to-end adversarial installation contract

**Files:**

- Modify: `tests/cli/test_ci.py`
- Modify: `tests/test_github_ci_audit.py`
- Modify: `tests/test_github_ci_bootstrap.py`

- [ ] **Step 1: Add an init-to-audit round-trip test**

Exercise only public CLI entry points:

```python
def test_init_github_then_ci_audit_round_trips_without_loading_lattice(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    initialized = runner.invoke(
        app,
        ["init", "--github", "--repository", "Guardantix/doc-lattice"],
    )
    audited = runner.invoke(
        app,
        ["ci", "audit", "--repository", "guardantix/DOC-LATTICE"],
    )

    assert initialized.exit_code == 0
    assert audited.exit_code == 0
    assert audited.stdout == "doc-lattice ci audit: ok\n"
```

Patch the runtime's config and lattice loaders to raise if called so the test proves both GitHub
commands stay outside lattice loading and cache persistence.

- [ ] **Step 2: Add one mutation test per load-bearing generated control**

Starting from exact rendered artifacts, mutate one structural field at a time and assert audit exits
`1` for:

- Adding `pull_request_target`.
- Adding a PR trigger to the Linear workflow.
- Removing or broadening the Linear repository/ref/event condition.
- Renaming the `linear` job.
- Removing `environment: doc-lattice-linear`.
- Moving the secret to job env or an earlier step.
- Replacing the dedicated environment secret with `secrets.LINEAR_API_KEY`.
- Adding another Linear-secret reference in an unrelated workflow.
- Removing checkout `persist-credentials: false`.
- Replacing either action SHA with a tag.
- Enabling setup-uv cache or adding `actions/cache`.
- Adding real `reconcile` to a PR workflow.
- Deleting the durable bootstrap script.

For each case, assert a specific finding code rather than only checking the process return value.
Keep unrelated release workflow controls as a no-finding counterexample.

- [ ] **Step 3: Add current and pre-change GitHub ref semantics fixtures**

The tests do not attempt to emulate GitHub authorization. Instead, pin the inputs assumed by the
design and the audit control that covers the exception:

```python
@pytest.mark.parametrize(
    ("event", "github_ref", "environment_main_matches"),
    [
        ("pull_request", "refs/pull/17/merge", False),
        ("pull_request_review", "refs/pull/17/merge", False),
        ("pull_request_review_comment", "refs/pull/17/merge", False),
        ("pull_request_target", "refs/heads/main", True),
    ],
)
def test_documented_github_ref_security_model(event, github_ref, environment_main_matches):
    assert (github_ref == "refs/heads/main") is environment_main_matches
    if event == "pull_request_target":
        workflow = parse_workflow(
            Path(".github/workflows/unsafe.yml"),
            "on: pull_request_target\njobs: {}\n",
        )
        assert {finding.code for finding in audit_global_workflows((workflow,))} == {
            "PULL_REQUEST_TARGET"
        }
```

Add a pre-change head-ref fixture showing that a head branch literally named `main` would match;
this test documents why GitHub Enterprise Server remains unsupported rather than asserting the
current environment rule repairs it.

- [ ] **Step 4: Add bootstrap command-order and no-secret regression assertions**

For every fake-gh state, assert all mutating `PUT`/`POST` vectors occur only after the confirmation
read, policy read-back occurs after mutation, and the `gh secret set` text occurs after that
read-back. Assert the script never invokes `gh secret set` or `gh secret delete` itself; it only
prints those commands for the maintainer. Search captured stdout, stderr, argv logs, and state JSON
for the Linear fixture prefix and require no match.

- [ ] **Step 5: Run the end-to-end security regression slice**

Run:

```bash
uv run --group dev pytest --no-cov tests/test_github_ci_audit.py tests/test_github_ci_bootstrap.py tests/cli/test_ci.py tests/cli/test_init.py -v
```

Expected: every adversarial mutation fails for its intended reason, counterexamples remain clean,
and the init-to-audit round trip passes. This focused slice intentionally disables repository-wide
coverage enforcement; the full suite below remains the authoritative coverage gate.

- [ ] **Step 6: Commit the end-to-end security contract**

```bash
git add tests/test_github_ci_audit.py tests/test_github_ci_bootstrap.py tests/cli/test_ci.py
git commit -m "Lock GitHub CI security regressions"
```

## Task 10: Publish the user contract and durable architecture decision

**Files:**

- Modify: `README.md:206-247`
- Modify: `README.md:445-499`
- Modify: `ARCHITECTURE.md:319-end`
- Modify: `CHANGELOG.md:7-8`
- Modify: `tests/test_package_metadata.py`

- [ ] **Step 1: Write failing documentation-contract tests**

Extend `tests/test_package_metadata.py` with assertions that prevent the migration ordering from
drifting:

```python
def test_supported_docs_order_github_linear_secret_after_verified_policy():
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.count("gh secret set DOC_LATTICE_LINEAR_API_KEY") == 1
    verified = readme.index("environment policy verified")
    secret_set = readme.index("gh secret set DOC_LATTICE_LINEAR_API_KEY")
    assert verified < secret_set
    assert "gh secret delete LINEAR_API_KEY --repo" in readme
    assert "gh secret delete DOC_LATTICE_LINEAR_API_KEY --repo" in readme
    assert "pull_request_target" in readme
    assert "ci audit is meaningful only after" in readme


def test_architecture_records_external_github_administration_boundary():
    architecture = (_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "reviewed external `gh` script" in architecture
    assert "GitHub environment" in architecture
    assert "linear_client" in architecture
    assert "doc_lattice.github_ci" in architecture
```

- [ ] **Step 2: Run the documentation tests to verify they fail**

Run:

```bash
uv run --group dev pytest --no-cov tests/test_package_metadata.py -v
```

Expected: the new README and architecture assertions fail. This focused metadata-only suite imports
no production code, so it disables coverage; the complete full-suite run is the authoritative
coverage gate.

- [ ] **Step 3: Update README command and adoption documentation**

Update the command table with:

- `init --github --repository OWNER/REPO` and its create-only managed artifacts.
- `ci audit [--repository OWNER/REPO]` with exit `1` for findings and `2` for unreadable/ambiguous
  state.
- `ci refresh --repository OWNER/REPO [--apply]` with preview `1`, tool/refusal `2`, and no
  noninteractive apply bypass.

Update the general exit-code table so `1` means a coherent policy/gate finding or a refresh update,
not only a lattice finding. Keep `2` as invalid, unreadable, unsafe, or unreliable tool state.

Replace “Every command except init accepts --config” with a positive list of lattice-loading
commands. State that `init` and both `ci` subcommands deliberately do not accept config or load the
lattice.

Expand “Adopting doc-lattice” into ordinary offline setup plus explicit managed GitHub setup. The
managed sequence is exactly:

```bash
uvx --python 3.13 --from doc-lattice==2.0.0 doc-lattice init \
  --github --repository OWNER/REPO
bash .github/doc-lattice-bootstrap.sh plan OWNER/REPO
bash .github/doc-lattice-bootstrap.sh apply OWNER/REPO

# Continue only after apply prints: environment policy verified
gh secret set DOC_LATTICE_LINEAR_API_KEY \
  --env doc-lattice-linear --repo OWNER/REPO

# Run either deletion only when plan/apply reported that repository-scoped name.
gh secret delete LINEAR_API_KEY --repo OWNER/REPO
gh secret delete DOC_LATTICE_LINEAR_API_KEY --repo OWNER/REPO

bash .github/doc-lattice-bootstrap.sh verify OWNER/REPO
uvx --python 3.13 --from doc-lattice==2.0.0 doc-lattice ci audit \
  --repository OWNER/REPO
```

Immediately explain that the secret-setting command is not ready before `apply` re-reads the exact
`main`-only policy; `apply` never receives the key. Explain expected bootstrap exit codes, including
first-time apply `1`, and instruct existing adopters to rotate or obtain a key, remove the old
hand-written Linear workflow in the same reviewed change, delete both broader secret names, and
obtain organization-owner confirmation when org secret visibility is unavailable.

Document plan availability, GitHub.com-only support, canonical-case verification, the required
durable bootstrap path, merge-queue non-support, post-adoption-only audit, refresh for upgrades and
renames, direct-command heuristic limits, no generated real reconcile, and the environment as the
authorization boundary. Record the `pull_request_target` exception and trusted-main residual risk
without duplicating the full design spec. State that the final-version syntax gate rejects pins that
can never be releases but does not prove that a release is already published or that an unreleased
source checkout matches it. Describe required environment reviewers and administrator-bypass
restrictions as optional manually administered controls outside the initial generated script.
Document Bash 3.2 and GitHub CLI as requirements, macOS/Linux plus Git Bash or WSL as supported
execution environments, and native PowerShell as unsupported by the initial script.

- [ ] **Step 4: Add architecture decision AD-16**

Append an accepted decision dated 2026-07-15 with:

- Context: workflow files are repository-controlled, same-repository PRs can expose broad secrets,
  and normal package code must not receive GitHub administration credentials.
- Decision: `doc_lattice.github_ci` renders and audits; CLI filesystem adapters stay offline; a
  reviewed external `gh` script configures and verifies an exact-main GitHub environment; the
  environment-only dedicated secret is mapped only on the final Linear step; audit bans
  `pull_request_target`; real reconcile is never generated.
- Consequences: `linear_client` remains the only Python network module, remote setup is explicit and
  resumable, local audit cannot see environment or organization drift, and trusted-main governance
  remains the residual boundary.

- [ ] **Step 5: Update Unreleased changelog**

Under `## [Unreleased]`, add an `### Added` section covering explicit GitHub generation, protected
Linear bootstrap, `ci audit`, and managed refresh. Add a `### Security` section stating that the
generated Linear workflow uses an environment-only credential and that existing repository-scoped
`LINEAR_API_KEY` installations must migrate and rotate/delete the broader key.

- [ ] **Step 6: Run documentation and version guards**

Run:

```bash
uv run --group dev pytest --no-cov tests/test_package_metadata.py -v
UV_CACHE_DIR=/tmp/doc-lattice-uv-cache uv run --group dev python scripts/check_version_sync.py
git diff --check
```

Expected: documentation-contract tests and version sync pass; diff check prints nothing. The
complete full-suite run remains the authoritative coverage gate.

- [ ] **Step 7: Commit supported documentation**

```bash
git add README.md ARCHITECTURE.md CHANGELOG.md tests/test_package_metadata.py
git commit -m "Document protected GitHub Linear installation"
```

## Task 11: Run complete verification and review the generated artifacts

**Files:**

- Verify only; fix failures in their owning files and amend the corresponding task commit.

- [ ] **Step 1: Run the full test suite**

```bash
uv run --group dev pytest
```

Expected: all tests pass and coverage remains at least 80 percent.

- [ ] **Step 2: Run all production static gates**

```bash
uv run --group dev ruff check src tests
uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev python scripts/check_version_sync.py
```

Expected: every command exits `0`; the typing-boundary script reports Any/cast confined to boundary
modules.

- [ ] **Step 3: Build and inspect the source distribution**

Run:

```bash
uv build --sdist --no-build-isolation --out-dir /tmp/doc-lattice-github-ci-dist
uv run --group dev pytest tests/test_package_metadata.py::test_built_sdist_contains_only_publishable_source_files -v
```

Expected: one valid sdist is built and the package-content test passes with the new package modules
included under `src/`.

- [ ] **Step 4: Render a fresh installation and syntax-check the exact script**

Use a pytest-owned temporary directory through the end-to-end test rather than modifying this
repository:

```bash
uv run --group dev pytest tests/cli/test_ci.py::test_init_github_then_ci_audit_round_trips_without_loading_lattice tests/test_github_ci_bootstrap.py::test_rendered_bootstrap_is_the_third_managed_artifact_and_is_valid_bash -v
```

Expected: generated files audit cleanly and Bash syntax exits `0`.

- [ ] **Step 5: Inspect repository state and the commit series**

```bash
git status --short --branch
git log --oneline --decorate -12
git diff --check main...HEAD
```

Expected: the worktree is clean, commits are scoped by task, and the branch diff has no whitespace
errors.

- [ ] **Step 6: Request code review before integration**

Use `superpowers:requesting-code-review` against the approved design and this plan. Address any
security or behavior findings with the receiving-code-review workflow, rerun the complete
verification set, and only then use `superpowers:finishing-a-development-branch` to choose PR,
merge, or cleanup.
