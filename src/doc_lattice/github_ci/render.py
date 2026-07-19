"""Deterministic rendering for managed GitHub Actions artifacts."""

import re
from pathlib import PurePosixPath

from ..scaffold import PYTHON_PIN
from .identity import parse_repository, validate_final_release_version
from .model import (
    ARTIFACT_MARKER_PREFIX,
    BOOTSTRAP_SHEBANG,
    MANAGED_SCHEMA_LINE,
    REPOSITORY_MARKER_PREFIX,
    VERSION_MARKER_PREFIX,
    ArtifactRole,
    ManagedArtifact,
    ManagedArtifactTarget,
)

OFFLINE_WORKFLOW_PATH = PurePosixPath(".github/workflows/doc-lattice.yml")
LINEAR_WORKFLOW_PATH = PurePosixPath(".github/workflows/doc-lattice-linear.yml")
BOOTSTRAP_PATH = PurePosixPath(".github/doc-lattice-bootstrap.sh")
GIT_ATTRIBUTES_PATH = PurePosixPath(".github/.gitattributes")
BOOTSTRAP_EOL_RULE = "doc-lattice-bootstrap.sh text eol=lf"

CANONICAL_ARTIFACT_TARGETS = (
    ManagedArtifactTarget("offline", OFFLINE_WORKFLOW_PATH),
    ManagedArtifactTarget("linear", LINEAR_WORKFLOW_PATH),
    ManagedArtifactTarget("bootstrap", BOOTSTRAP_PATH),
    ManagedArtifactTarget("attributes", GIT_ATTRIBUTES_PATH),
)

CHECKOUT_REF = "34e114876b0b11c390a56381ad16ebd13914f8d5"  # pragma: allowlist secret
SETUP_UV_REF = "d0cc045d04ccac9d8b7881df0226f9e82c39688e"  # pragma: allowlist secret

# The trusted Linear step maps the dedicated environment secret onto this fixed environment
# variable. Rendering and the audit trusted-slot exemption both read these constants so a
# template edit cannot leave the exemption stale and fail a correct install.
LINEAR_JOB_ID = "linear"
LINEAR_SECRET_ENV_NAME = "LINEAR_API_KEY"  # noqa: S105  # pragma: allowlist secret
LINEAR_SECRET_ENV_VALUE = "${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}"  # noqa: S105  # pragma: allowlist secret

_TOKEN_RE = re.compile(r"__(?:REPOSITORY|VERSION|CHECKOUT_REF|SETUP_UV_REF|PYTHON_PIN)__")

_OFFLINE_TEMPLATE = (
    """name: doc-lattice
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
"""
    "          uvx --python __PYTHON_PIN__ --from doc-lattice==__VERSION__ "
    "doc-lattice ci audit --repository __REPOSITORY__\n"
    """          rc_audit=$?
          uvx --python __PYTHON_PIN__ --from doc-lattice==__VERSION__ doc-lattice check
          rc_check=$?
          uvx --python __PYTHON_PIN__ --from doc-lattice==__VERSION__ doc-lattice lint
          rc_lint=$?
          [ "$rc_audit" -eq 0 ] && [ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ]
"""
)

_LINEAR_TEMPLATE = (
    """name: doc-lattice Linear
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
jobs:
"""
    f"  {LINEAR_JOB_ID}:\n"
    """    name: Trusted Linear gate
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
          uv python install __PYTHON_PIN__
          uv venv --python __PYTHON_PIN__ "$RUNNER_TEMP/doc-lattice-venv"
          uv pip install --python "$RUNNER_TEMP/doc-lattice-venv/bin/python" doc-lattice=="""
    "__VERSION__\n"
    """      - name: Run trusted Linear gate
        env:
"""
    f"          {LINEAR_SECRET_ENV_NAME}: {LINEAR_SECRET_ENV_VALUE}\n"
    """        run: '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear --exit-code'
"""
)

_BOOTSTRAP_TEMPLATE = """set -u
EXPECTED_REPOSITORY='__REPOSITORY__'
ENVIRONMENT='doc-lattice-linear'
EXIT_FINDING=1
EXIT_TOOL_ERROR=2

die() { printf 'error: %s\n' "$1" >&2; exit "$EXIT_TOOL_ERROR"; }
lower_ascii() {
  printf '%s' "$1" | LC_ALL=C tr \
    'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz'
}
api() { gh api --hostname github.com "$@"; }
confirm_repository() {
  [ -t 0 ] || die "apply requires an interactive TTY on stdin"
  printf 'Type %s to apply: ' "$CANONICAL_REPOSITORY" >&2
  IFS= read -r answer || die "confirmation ended before a repository was entered"
  [ "$answer" = "$CANONICAL_REPOSITORY" ] || die "repository confirmation did not match"
}

REINSPECTING=0
inspection_die() {
  if [ "$REINSPECTING" -eq 1 ]; then
    die "$1; run a fresh plan then apply"
  fi
  die "$1"
}

sort_lines() {
  printf '%s\n' "$1" | LC_ALL=C sort
}

contains_line() {
  [ -n "$1" ] || return 1
  while IFS= read -r candidate; do
    [ "$candidate" = "$2" ] && return 0
  done <<EOF
$1
EOF
  return 1
}

contains_ascii_case_line() {
  [ -n "$1" ] || return 1
  line_target_key=$(lower_ascii "$2") || return 2
  while IFS= read -r candidate; do
    line_candidate_key=$(lower_ascii "$candidate") || return 2
    [ "$line_candidate_key" = "$line_target_key" ] && return 0
  done <<EOF
$1
EOF
  return 1
}

append_fingerprint() { STATE_FINGERPRINT="${STATE_FINGERPRINT}${#1}:$1"; }

load_repository() {
  repository_fields=$(api "repos/$REQUESTED_REPOSITORY" --jq \
    '[.full_name, .default_branch, .visibility, .owner.type] | @tsv') || \
    inspection_die "repository metadata inspection failed"
  IFS=$'\t' read -r CANONICAL_REPOSITORY DEFAULT_BRANCH VISIBILITY OWNER_TYPE <<EOF
$repository_fields
EOF
  [ -n "$CANONICAL_REPOSITORY" ] && [ -n "$DEFAULT_BRANCH" ] && \
    [ -n "$VISIBILITY" ] && [ -n "$OWNER_TYPE" ] || \
    inspection_die "repository metadata was incomplete"
  printf 'canonical repository: %s\n' "$CANONICAL_REPOSITORY"
  [ "$CANONICAL_REPOSITORY" = "$EXPECTED_REPOSITORY" ] || \
    inspection_die "canonical repository identity does not match embedded repository"
  [ "$DEFAULT_BRANCH" = "main" ] || \
    inspection_die "default branch must be exactly main"
  OWNER=${CANONICAL_REPOSITORY%%/*}
  REPOSITORY=${CANONICAL_REPOSITORY#*/}
}

check_eligibility() {
  PLAN_NAME=""
  case "$VISIBILITY:$OWNER_TYPE" in
    public:*)
      ELIGIBILITY="public repository"
      ;;
    internal:User)
      inspection_die "user-owned internal repositories are not eligible"
      ;;
    private:User)
      PLAN_NAME=$(api "users/$OWNER" --jq '.plan.name') || \
        inspection_die "personal plan metadata is unavailable; eligibility is unverified"
      [ "$PLAN_NAME" = "pro" ] || \
        inspection_die "personal private repositories require the pro plan"
      ELIGIBILITY="personal pro plan"
      ;;
    private:Organization|internal:Organization)
      PLAN_NAME=$(api "orgs/$OWNER" --jq '.plan.name') || \
        inspection_die "organization plan metadata is unavailable; eligibility is unverified"
      case "$PLAN_NAME" in
        team|enterprise|business|business_plus)
          ELIGIBILITY="organization $PLAN_NAME plan"
          ;;
        *)
          inspection_die "organization plan is not recognized as eligible"
          ;;
      esac
      ;;
    *)
      inspection_die "repository visibility or owner type is not recognized as eligible"
      ;;
  esac
  printf 'eligibility: %s\n' "$ELIGIBILITY"
}

load_state() {
  REPOSITORY_SECRET_NAMES=$(api \
    "repos/$OWNER/$REPOSITORY/actions/secrets" --paginate --jq '.secrets[].name') || \
    inspection_die "repository secret-name metadata inspection failed; state is unreliable"
  REPOSITORY_SECRET_NAMES=$(sort_lines "$REPOSITORY_SECRET_NAMES") || \
    inspection_die "repository secret-name normalization failed; state is unreliable"

  ENVIRONMENT_NAMES=$(api "repos/$OWNER/$REPOSITORY/environments" --paginate \
    --jq '.environments[].name') || \
    inspection_die "environment metadata inspection failed; state is unreliable"
  ENVIRONMENT_NAMES=$(sort_lines "$ENVIRONMENT_NAMES") || \
    inspection_die "environment-name normalization failed; state is unreliable"

  ENVIRONMENT_EXISTS=0
  CUSTOM_BRANCH_POLICIES=""
  PROTECTED_BRANCHES=""
  POLICY_ROWS=""
  ENVIRONMENT_SECRET_NAMES=""
  contains_ascii_case_line "$ENVIRONMENT_NAMES" "$ENVIRONMENT"
  environment_name_status=$?
  [ "$environment_name_status" -le 1 ] || \
    inspection_die "environment-name comparison failed; state is unreliable"
  if [ "$environment_name_status" -eq 0 ]; then
    ENVIRONMENT_EXISTS=1
    environment_fields=$(api "repos/$OWNER/$REPOSITORY/environments/$ENVIRONMENT" --jq \
      '[.deployment_branch_policy.custom_branch_policies, \
.deployment_branch_policy.protected_branches] | @tsv') || \
      inspection_die "target environment policy inspection failed; state is unreliable"
    IFS=$'\t' read -r CUSTOM_BRANCH_POLICIES PROTECTED_BRANCHES <<EOF
$environment_fields
EOF
    POLICY_ROWS=$(api \
      "repos/$OWNER/$REPOSITORY/environments/$ENVIRONMENT/deployment-branch-policies" \
      --paginate --jq '.branch_policies[] | [.name, .type] | @tsv') || \
      inspection_die "deployment branch-policy inspection failed; state is unreliable"
    POLICY_ROWS=$(sort_lines "$POLICY_ROWS") || \
      inspection_die "branch-policy normalization failed; state is unreliable"
    ENVIRONMENT_SECRET_NAMES=$(api \
      "repos/$OWNER/$REPOSITORY/environments/$ENVIRONMENT/secrets" \
      --paginate --jq '.secrets[].name') || \
      inspection_die "environment secret-name metadata inspection failed; state is unreliable"
    ENVIRONMENT_SECRET_NAMES=$(sort_lines "$ENVIRONMENT_SECRET_NAMES") || \
      inspection_die "environment secret-name normalization failed; state is unreliable"
  fi

  ENVIRONMENT_SECRET_PRESENT=0
  LEGACY_REPO_SECRET_PRESENT=0
  DEDICATED_REPO_SECRET_PRESENT=0
  contains_line "$ENVIRONMENT_SECRET_NAMES" "DOC_LATTICE_LINEAR_API_KEY" && \
    ENVIRONMENT_SECRET_PRESENT=1
  contains_line "$REPOSITORY_SECRET_NAMES" "LINEAR_API_KEY" && \
    LEGACY_REPO_SECRET_PRESENT=1
  contains_line "$REPOSITORY_SECRET_NAMES" "DOC_LATTICE_LINEAR_API_KEY" && \
    DEDICATED_REPO_SECRET_PRESENT=1

  POLICY_EXACT=0
  SAFE_INCOMPLETE=0
  if [ "$CUSTOM_BRANCH_POLICIES" = "true" ] && [ "$PROTECTED_BRANCHES" = "false" ]; then
    if [ "$POLICY_ROWS" = $'main\tbranch' ]; then
      POLICY_EXACT=1
    elif [ -z "$POLICY_ROWS" ] && [ -z "$ENVIRONMENT_SECRET_NAMES" ]; then
      SAFE_INCOMPLETE=1
    fi
  fi

  STATE_FINGERPRINT=""
  append_fingerprint "$CANONICAL_REPOSITORY"
  append_fingerprint "$DEFAULT_BRANCH"
  append_fingerprint "$VISIBILITY"
  append_fingerprint "$OWNER_TYPE"
  append_fingerprint "$PLAN_NAME"
  append_fingerprint "$ELIGIBILITY"
  append_fingerprint "$REPOSITORY_SECRET_NAMES"
  append_fingerprint "$ENVIRONMENT_NAMES"
  append_fingerprint "$ENVIRONMENT_EXISTS"
  append_fingerprint "$CUSTOM_BRANCH_POLICIES"
  append_fingerprint "$PROTECTED_BRANCHES"
  append_fingerprint "$POLICY_ROWS"
  append_fingerprint "$ENVIRONMENT_SECRET_NAMES"
}

print_state() {
  printf 'target: %s environment %s\n' "$CANONICAL_REPOSITORY" "$ENVIRONMENT"
  printf 'eligibility result: %s\n' "$ELIGIBILITY"
  if [ "$POLICY_EXACT" -eq 1 ]; then
    printf 'policy: exact\n'
  elif [ "$ENVIRONMENT_EXISTS" -eq 0 ]; then
    printf 'policy: absent\n'
  elif [ "$SAFE_INCOMPLETE" -eq 1 ]; then
    printf 'policy: safe-incomplete\n'
  else
    printf 'policy: mismatch\n'
  fi
  if [ "$ENVIRONMENT_SECRET_PRESENT" -eq 1 ]; then
    printf 'environment secret DOC_LATTICE_LINEAR_API_KEY: present by name metadata\n'
  else
    printf 'environment secret DOC_LATTICE_LINEAR_API_KEY: absent by name metadata\n'
  fi
  if [ "$LEGACY_REPO_SECRET_PRESENT" -eq 1 ]; then
    printf 'repository secret LINEAR_API_KEY: present by name metadata\n'
  else
    printf 'repository secret LINEAR_API_KEY: absent by name metadata\n'
  fi
  if [ "$DEDICATED_REPO_SECRET_PRESENT" -eq 1 ]; then
    printf 'repository secret DOC_LATTICE_LINEAR_API_KEY: present by name metadata\n'
  else
    printf 'repository secret DOC_LATTICE_LINEAR_API_KEY: absent by name metadata\n'
  fi
  printf '%s\n' \
    'note: organization-secret visibility may require confirmation by the organization owner'
}

observable_status() {
  if [ "$POLICY_EXACT" -eq 1 ] && \
    [ "$ENVIRONMENT_SECRET_PRESENT" -eq 1 ] && \
    [ "$LEGACY_REPO_SECRET_PRESENT" -eq 0 ] && \
    [ "$DEDICATED_REPO_SECRET_PRESENT" -eq 0 ]; then
    return 0
  fi
  return "$EXIT_FINDING"
}

create_main_policy() {
  api --method POST \
    "repos/$OWNER/$REPOSITORY/environments/$ENVIRONMENT/deployment-branch-policies" \
    --field 'name=main' --field 'type=branch' >/dev/null
}

apply_policy() {
  mutated=0
  if [ "$POLICY_EXACT" -eq 1 ]; then
    :
  elif [ "$ENVIRONMENT_EXISTS" -eq 0 ]; then
    api --method PUT "repos/$OWNER/$REPOSITORY/environments/$ENVIRONMENT" \
      --field 'deployment_branch_policy[protected_branches]=false' \
      --field 'deployment_branch_policy[custom_branch_policies]=true' >/dev/null || \
      die "environment PUT did not complete observably; "\
"no branch-policy POST was attempted; rerun plan then apply"
    create_main_policy || \
      die "environment custom policy mode was created; "\
"main branch-policy POST did not complete observably; rerun plan then apply"
    mutated=1
  elif [ "$SAFE_INCOMPLETE" -eq 1 ]; then
    create_main_policy || \
      die "environment custom policy mode remains present with no reviewed policy; "\
"main branch-policy POST did not complete observably; rerun plan then apply"
    mutated=1
  else
    die "existing environment policy is ambiguous; refusing to narrow or take ownership"
  fi

  if [ "$mutated" -eq 1 ]; then
    load_state
    [ "$POLICY_EXACT" -eq 1 ] || \
      die "environment mutation did not read back as exact; rerun plan then apply"
  fi
  printf '%s\n' 'environment policy verified'
  if [ "$ENVIRONMENT_SECRET_PRESENT" -eq 0 ]; then
    printf '%s%s\n' \
      'gh secret set DOC_LATTICE_LINEAR_API_KEY --env doc-lattice-linear --repo ' \
      "$CANONICAL_REPOSITORY"
  fi
  if [ "$LEGACY_REPO_SECRET_PRESENT" -eq 1 ]; then
    printf '%s%s\n' \
      'gh secret delete LINEAR_API_KEY --repo ' "$CANONICAL_REPOSITORY"
  fi
  if [ "$DEDICATED_REPO_SECRET_PRESENT" -eq 1 ]; then
    printf '%s%s\n' \
      'gh secret delete DOC_LATTICE_LINEAR_API_KEY --repo ' "$CANONICAL_REPOSITORY"
  fi
}

[ "$#" -eq 2 ] || die "usage: $0 plan|apply|verify OWNER/REPO"
OPERATION=$1
REQUESTED_REPOSITORY=$2
case "$OPERATION" in
  plan|apply|verify) ;;
  *) die "operation must be exactly plan, apply, or verify" ;;
esac
REQUESTED_COMPARISON_KEY=$(lower_ascii "$REQUESTED_REPOSITORY") || \
  die "requested repository identity normalization failed"
EXPECTED_COMPARISON_KEY=$(lower_ascii "$EXPECTED_REPOSITORY") || \
  die "embedded repository identity normalization failed"
[ "$REQUESTED_COMPARISON_KEY" = "$EXPECTED_COMPARISON_KEY" ] || \
  die "requested repository does not match embedded repository"
command -v gh >/dev/null 2>&1 || die "gh is required on PATH"
gh auth status --hostname github.com >/dev/null 2>&1 || \
  die "gh authentication for github.com is required"

load_repository
check_eligibility
load_state
print_state

case "$OPERATION" in
  plan)
    observable_status
    exit $?
    ;;
  verify)
    printf '%s%s\n' \
      'reminder: run local doc-lattice ci audit; remote verification cannot prove ' \
      'legacy workflow removal'
    observable_status
    exit $?
    ;;
  apply)
    INITIAL_CANONICAL_REPOSITORY=$CANONICAL_REPOSITORY
    INITIAL_STATE_FINGERPRINT=$STATE_FINGERPRINT
    confirm_repository
    REINSPECTING=1
    load_repository
    check_eligibility
    load_state
    REINSPECTING=0
    [ "$CANONICAL_REPOSITORY" = "$INITIAL_CANONICAL_REPOSITORY" ] && \
      [ "$STATE_FINGERPRINT" = "$INITIAL_STATE_FINGERPRINT" ] || \
      die "repository state changed during confirmation; run a fresh plan then apply"
    apply_policy
    observable_status
    exit $?
    ;;
esac
"""


def ownership_header(role: ArtifactRole, repository: str, version: str) -> str:
    """Build the ownership header for one managed artifact.

    Args:
        role: Managed artifact role.
        repository: Validated repository display identity.
        version: Validated final-release version.

    Returns:
        The four-line ownership header with a final newline.
    """
    return (
        f"{MANAGED_SCHEMA_LINE}\n"
        f"{ARTIFACT_MARKER_PREFIX} {role}\n"
        f"{VERSION_MARKER_PREFIX} {version}\n"
        f"{REPOSITORY_MARKER_PREFIX} {repository}\n"
    )


def render_workflows(repository: str, version: str) -> tuple[ManagedArtifact, ManagedArtifact]:
    """Render the offline and trusted Linear GitHub Actions workflows.

    Args:
        repository: GitHub repository in ``OWNER/REPO`` form.
        version: Exact final-release version to install in both workflows.

    Returns:
        The offline and Linear workflow artifacts in canonical order.

    Raises:
        ConfigError: If the repository or version is invalid.
    """
    identity = parse_repository(repository)
    validate_final_release_version(version)

    offline_text = ownership_header("offline", identity.display, version) + _replace_tokens(
        _OFFLINE_TEMPLATE,
        identity.display,
        version,
    )
    linear_text = ownership_header("linear", identity.display, version) + _replace_tokens(
        _LINEAR_TEMPLATE,
        identity.display,
        version,
    )
    return (
        ManagedArtifact("offline", OFFLINE_WORKFLOW_PATH, offline_text),
        ManagedArtifact("linear", LINEAR_WORKFLOW_PATH, linear_text),
    )


def render_bootstrap(repository: str, version: str) -> ManagedArtifact:
    """Render the human-run GitHub environment bootstrap script.

    Args:
        repository: GitHub repository in ``OWNER/REPO`` form.
        version: Exact final-release version recorded in the ownership header.

    Returns:
        The Bash 3.2 bootstrap artifact.

    Raises:
        ConfigError: If the repository or version is invalid.
    """
    identity = parse_repository(repository)
    validate_final_release_version(version)
    text = (
        f"{BOOTSTRAP_SHEBANG}\n"
        + ownership_header("bootstrap", identity.display, version)
        + _replace_tokens(_BOOTSTRAP_TEMPLATE, identity.display, version)
    )
    return ManagedArtifact("bootstrap", BOOTSTRAP_PATH, text)


def render_git_attributes(repository: str, version: str) -> ManagedArtifact:
    """Render the scoped policy that preserves bootstrap LF line endings.

    Args:
        repository: GitHub repository in ``OWNER/REPO`` form.
        version: Exact final-release version recorded in the ownership header.

    Returns:
        The managed ``.github/.gitattributes`` artifact.

    Raises:
        ConfigError: If the repository or version is invalid.
    """
    identity = parse_repository(repository)
    validate_final_release_version(version)
    text = ownership_header("attributes", identity.display, version) + f"{BOOTSTRAP_EOL_RULE}\n"
    return ManagedArtifact("attributes", GIT_ATTRIBUTES_PATH, text)


def render_managed_artifacts(
    repository: str,
    version: str,
) -> tuple[ManagedArtifact, ManagedArtifact, ManagedArtifact, ManagedArtifact]:
    """Render every managed GitHub CI artifact in canonical order.

    Args:
        repository: GitHub repository in ``OWNER/REPO`` form.
        version: Exact final-release version for the managed artifacts.

    Returns:
        The offline workflow, Linear workflow, human-run bootstrap script, and scoped
        Git attributes policy.

    Raises:
        ConfigError: If the repository or version is invalid.
    """
    offline, linear = render_workflows(repository, version)
    return (
        offline,
        linear,
        render_bootstrap(repository, version),
        render_git_attributes(repository, version),
    )


def _replace_tokens(template: str, repository: str, version: str) -> str:
    """Replace the fixed renderer tokens without interpreting literal braces."""
    replacements = {
        "__REPOSITORY__": repository,
        "__VERSION__": version,
        "__CHECKOUT_REF__": CHECKOUT_REF,
        "__SETUP_UV_REF__": SETUP_UV_REF,
        "__PYTHON_PIN__": PYTHON_PIN,
    }
    return _TOKEN_RE.sub(lambda match: replacements[match.group(0)], template)
