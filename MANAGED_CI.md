# Managed GitHub and Linear setup

> **Deprecated in 4.x, removed in 5.0.** `init --github`, `ci audit`, and `ci refresh` are
> deprecated. They keep working, byte for byte, through every 4.x release, and 5.0 removes them.
> The [hand-installable recipe](#the-hand-installable-recipe) below replaces them, and it is what
> a new adoption should install.
>
> An installed managed setup does not break on its own when 5.0 ships. It keeps running the exact
> 4.x version its workflows pin, which is the trap: it will sit on an unsupported release until
> someone converts it. The managed offline workflow also runs `ci audit`, so pinning it forward to
> 5.0 is what actually fails. Convert while 4.x is still supported, using
> [Converting a managed installation to the recipe](#converting-a-managed-installation-to-the-recipe).

This document covers two setups that produce the same protected Linear reporting.

The **recipe** is the supported one. You install three files by hand, you own them, and the
protection comes from a GitHub environment whose deployment allow list is exactly `main` plus a
workflow that maps the dedicated secret only onto its final step.

The **managed setup** is the deprecated product. A human maintainer generated and reviewed four
committed, create-only artifacts: two GitHub Actions workflows, a bootstrap script that configures
the protected GitHub environment, and a scoped `.gitattributes` file. It added drift detection,
byte-level refresh, and a scripted remote readback on top of the same boundary. Those additions,
not the boundary, are what the recipe gives up.

Both setups share the [requirements](#requirements) and the [security model](#security-model)
below.

## The hand-installable recipe

### What you install

- `.doc-lattice.yml`, scaffolded by plain `init`.
- `.github/workflows/doc-lattice.yml`, the offline check and lint gate that plain `init` prints.
  You own it, and it runs no network command and touches no secret.
- `.github/workflows/doc-lattice-linear.yml`, the trusted Linear gate. Plain `init` does not print
  this one, so this document supplies it in full below.
- A `doc-lattice-linear` GitHub environment whose deployment allow list is exactly the `main`
  branch, holding one secret named `DOC_LATTICE_LINEAR_API_KEY`.

There is no bootstrap script and no `.github/.gitattributes` rule in the recipe. Both existed to
support the managed bootstrap script, and the recipe has none.

Run every step from reviewed, trusted project state, and land the whole setup as one reviewed
change.

### 1. Scaffold the config and the offline workflow

```bash
uvx --python 3.13 --from doc-lattice==4.1.0 doc-lattice init
```

Do not pass `--github`. Plain `init` writes `.doc-lattice.yml` when it is absent and prints three
blocks: the `.gitignore` lines, the pre-commit hooks, and the offline workflow. Save the printed
workflow as `.github/workflows/doc-lattice.yml`.

That workflow carries the two pinned `uses:` lines the release ships. The Linear workflow in the
next step must carry the same two pins, so keep this output until step 2 is done.

### 2. Add the trusted Linear workflow

Save this as `.github/workflows/doc-lattice-linear.yml`, replacing `OWNER/REPO` with your exact
canonical GitHub repository identity, including its display casing:

```yaml
name: doc-lattice Linear
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
      github.repository == 'OWNER/REPO' &&
      github.ref == 'refs/heads/main' &&
      (github.event_name == 'push' || github.event_name == 'workflow_dispatch')
    environment: doc-lattice-linear
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e # v6.8.0
        with:
          enable-cache: false
      - name: Install pinned doc-lattice without the Linear secret
        run: |
          uv python install 3.13
          uv venv --python 3.13 "$RUNNER_TEMP/doc-lattice-venv"
          uv pip install --python "$RUNNER_TEMP/doc-lattice-venv/bin/python" doc-lattice==4.1.0
      - name: Run trusted Linear gate
        env:
          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}
        run: '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear --exit-code'
```

Every line of that job is load-bearing, and no part of it is checked for you after 5.0:

- The trigger set is `push` to `main` plus `workflow_dispatch`, and nothing else. Never add a
  pull-request-family trigger to this file. `pull_request_target` in particular resolves
  `GITHUB_REF` to the default branch, so the environment policy would authorize it while it
  handles untrusted input.
- The `if:` guard is three conditions, not two. Repository and ref alone are not enough, because
  the event allowlist is what refuses a pull-request-family event that satisfies both. Keep all
  three.
- `environment: doc-lattice-linear` is the binding that grants secret access at all. Removing it
  removes access.
- The secret is mapped to `LINEAR_API_KEY` only in the `env:` of the final step. Do not promote it
  to a job-level or workflow-level `env:`, and do not add steps after it.
- The install step runs before the secret exists in the environment, so package resolution never
  happens with the key present.
- Both actions are pinned by commit SHA with a trailing version comment, `persist-credentials:
  false` keeps the job token out of `.git/config`, and `enable-cache: false` keeps a cross-run
  cache another workflow could populate out of the gate.
- `permissions: contents: read` is the whole token scope this job needs.

### 3. Create the protected environment

Requires an authenticated `gh` and repository owner or administrator authority. Substitute your
canonical `OWNER/REPO` throughout.

```bash
gh api --hostname github.com --method PUT \
  "repos/OWNER/REPO/environments/doc-lattice-linear" \
  --field 'deployment_branch_policy[protected_branches]=false' \
  --field 'deployment_branch_policy[custom_branch_policies]=true'

gh api --hostname github.com --method POST \
  "repos/OWNER/REPO/environments/doc-lattice-linear/deployment-branch-policies" \
  --field 'name=main' --field 'type=branch'
```

Read the policy back before going near the secret:

```bash
gh api --hostname github.com \
  "repos/OWNER/REPO/environments/doc-lattice-linear" \
  --jq '[.deployment_branch_policy.protected_branches,
         .deployment_branch_policy.custom_branch_policies] | @tsv'

gh api --hostname github.com --paginate \
  "repos/OWNER/REPO/environments/doc-lattice-linear/deployment-branch-policies" \
  --jq '.branch_policies[] | [.name, .type] | @tsv'
```

The first command must print `false` then `true`. The second must print exactly one row, `main`
then `branch`. Anything else means the policy is not the one this design depends on: stop, and do
not continue to step 4. If the environment already existed with broader or ambiguous rules, do not
narrow it blindly. Decide deliberately whether that environment is yours to take over.

### 4. Set the environment secret and remove repository-scoped copies

Only after the readback above is exactly right:

```bash
gh secret set DOC_LATTICE_LINEAR_API_KEY --env doc-lattice-linear --repo OWNER/REPO
```

`gh secret set` prompts for the value or reads it from stdin, so the key is never part of the
command arguments.

A repository-scoped Linear key defeats the whole boundary, because every workflow in the
repository can read it. List what exists and delete both names if either is present:

```bash
gh secret list --repo OWNER/REPO
gh secret delete LINEAR_API_KEY --repo OWNER/REPO
gh secret delete DOC_LATTICE_LINEAR_API_KEY --repo OWNER/REPO
```

If you are converting an existing installation that used a repository-scoped `LINEAR_API_KEY`,
rotate the key out of band rather than reusing it. The broader key may already have been exposed.
Repository administrators cannot always inspect organization secret visibility, so obtain
organization-owner confirmation that neither name is exposed to this repository, or have the owner
remove or exclude it.

### 5. Establish the reconcile baseline, on an initial adoption only

Annotate your documents, then run this once in the same reviewed change, before the workflows
reach `main` and the gates begin running. Commit the annotated input state first and run from an
otherwise clean working tree, so the reconcile-only diff stays reviewable and revertible:

```bash
uvx --python 3.13 --from doc-lattice==4.1.0 doc-lattice reconcile --all
```

A conversion from an existing installation skips this step. When it applies, what the clean-tree
precondition buys you and what the step does not promise are owned by
[README.md](README.md#adopting-doc-lattice-in-your-docs-repo); the selector semantics are owned by
[RECONCILE.md](RECONCILE.md).

### 6. Verify by hand

Nothing verifies a recipe installation for you. Run these yourself, and rerun them after any
policy, visibility, plan, rename, or transfer change:

```bash
gh api --hostname github.com --paginate \
  "repos/OWNER/REPO/environments/doc-lattice-linear/secrets" --jq '.secrets[].name'

gh secret list --repo OWNER/REPO
```

The first must print `DOC_LATTICE_LINEAR_API_KEY` and nothing else that carries a Linear key. The
second must not list `LINEAR_API_KEY` or `DOC_LATTICE_LINEAR_API_KEY` at repository scope. Then
repeat the two policy readbacks from step 3.

Review the rest by reading, because no command checks it:

- No workflow anywhere in the repository uses `pull_request_target`.
- No workflow other than `doc-lattice-linear.yml` references a Linear secret, by any spelling.
  Whole-context, wildcard, and computed `secrets` access all count, and a reusable-workflow job's
  `secrets: inherit` forwards every caller secret, so it counts too.
- `.github/workflows/doc-lattice-linear.yml` still matches the text in step 2, other than your
  repository identity and the pinned version.

### What the recipe keeps, and what it drops

It keeps the boundary. The GitHub environment is still the authoritative control: it allows only
the exact `main` branch, the dedicated secret exists only inside it, and that secret reaches
`LINEAR_API_KEY` only on the final step of the trusted job. The workflow-level guards, the action
pins, the least-privilege token, the disabled caching, and the rule that no generated workflow
runs real `reconcile` are all unchanged. Everything in [Security model](#security-model) below
applies to a recipe installation.

It drops the machinery that watched that boundary for you:

- **Repository-wide audit.** Nothing checks for `pull_request_target` or for another workflow
  reading a Linear secret. That check becomes the review discipline described in step 6.
- **Drift detection.** Nothing notices when someone edits the Linear workflow, weakens the `if:`
  guard, promotes the secret to a job-level `env:`, or adds a step after the final one.
- **Byte-level refresh.** No command upgrades the workflow, rewrites it after a repository rename,
  or refuses to move an installation backward. Upgrades are the manual procedure below.
- **Scripted remote readback.** There is no `plan`, `apply`, or `verify` that proves the remote
  environment policy, the environment secret, and the absence of repository-scoped copies in one
  run. The `gh` commands in steps 3 and 6 are the manual replacement, and running them is on you.
- **Ownership markers.** The two workflows are ordinary repository files with no managed identity,
  no version marker, and no create-only protection.

That is a real reduction in assurance, and it is the deliberate trade: the managed product had no
installations to justify maintaining it. The boundary the design actually rests on is the GitHub
environment, and the recipe keeps that intact.

### Upgrading a recipe installation

Nothing updates itself. Read the target release's section in [CHANGELOG.md](CHANGELOG.md) first,
because a release that changes generated output carries a `### Migration` subsection.

Print the target release's blocks and replace yours whole:

```bash
uvx --python 3.13 --from doc-lattice==NEW_VERSION doc-lattice init
```

Replace the pre-commit block and `.github/workflows/doc-lattice.yml` with the printed versions
rather than hand-editing their pinned versions. The blocks carry generated structure beyond the
pins, so bumping only the pins silently keeps an outdated shape.

Plain `init` cannot regenerate the Linear workflow, so replace that one from this document at the
target release: open `MANAGED_CI.md` for the release you are moving to and copy its step 2 block
whole, then reapply your repository identity. Do not bump only the `doc-lattice==` pin. For the
same reason the ordinary workflow is replaced whole, this workflow's structure and action pins can
change between releases independently of the version it installs.

### Converting a managed installation to the recipe

Nothing remote changes. The environment, its `main`-only policy, and
`DOC_LATTICE_LINEAR_API_KEY` are already exactly what the recipe wants, so leave them alone.

The local files change ownership from the tool to you, in one reviewed change:

1. Replace `.github/workflows/doc-lattice.yml`. The managed offline workflow runs `ci audit`,
   which 5.0 removes, so it cannot simply be carried forward. Run plain `init` at your current
   release and save the workflow it prints over the managed one.
2. Convert `.github/workflows/doc-lattice-linear.yml` by deleting its four ownership marker
   comment lines, the ones beginning `# doc-lattice-managed:`, `# doc-lattice-artifact:`,
   `# doc-lattice-version:`, and `# doc-lattice-repository:`. What remains is the step 2 workflow
   for the release you have installed. Compare it against step 2 in this document before
   committing.
3. Delete `.github/doc-lattice-bootstrap.sh`. Its remote work is already done, and its readback is
   replaced by the `gh` commands in step 6.
4. Delete `.github/.gitattributes` if it exists only for the bootstrap LF rule. Keep the file if
   your repository uses it for anything else, and drop just the
   `doc-lattice-bootstrap.sh text eol=lf` line.
5. Stop running `ci audit` and `ci refresh`, and adopt the manual review in step 6 in their place.

Run the step 6 verification once when the change lands.

## What the managed setup installs

> Deprecated. This section describes `init --github`, `ci audit`, and `ci refresh`, which 5.0
> removes. Use [the recipe](#the-hand-installable-recipe) for a new adoption.

- `.github/workflows/doc-lattice.yml` runs the offline audit, drift, and authority gates.
- `.github/workflows/doc-lattice-linear.yml` runs the Linear gate only on trusted `main`.
- `.github/doc-lattice-bootstrap.sh` configures and verifies the GitHub environment.
- `.github/.gitattributes` keeps the bootstrap script at LF line endings after checkout.

The bootstrap script is a durable managed artifact, not a disposable installer. Keep it committed
after installation. Bootstrap `verify` checks remote environment policy and secret-name metadata.
`ci audit` checks that the script is present and carries a valid ownership marker, version, and
repository identity, but it does not compare the bootstrap script byte for byte. `ci refresh`
performs the byte-level managed-artifact diff and can recreate a missing script after confirmation.

The scoped attributes file contains `doc-lattice-bootstrap.sh text eol=lf`, so a Windows checkout
with `core.autocrlf=true` does not turn the Git Bash script into unusable CRLF shell syntax. Audit
requires that exact effective rule while accepting either LF or CRLF separators in the attributes
file itself.

## Requirements

These apply to both setups, because they are GitHub's rules for environments rather than the
tool's. The recipe's `gh` steps assume the same authority the bootstrap script required.

The initial script supports GitHub.com repositories whose default branch is exactly `main`. It
requires Bash 3.2 or later and an authenticated GitHub CLI. The authenticated maintainer must be a
repository owner or administrator with authority to manage environments and inspect repository
secret names. Reading organization-plan metadata can require organization-owner or equivalent
`admin:org` authority; unavailable authority fails closed. Run the script on macOS or Linux, or on
Windows through Git Bash or WSL. Native PowerShell is not supported.

GitHub's [deployment and environment documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
defines environment availability and protection behavior. Public repositories are eligible on
current GitHub plans. Private repositories owned by a user require GitHub Pro; private or internal
organization repositories require GitHub Team or Enterprise. The script fails closed if
visibility, plan eligibility, canonical repository casing, the exact `main` default branch,
repository secret metadata, or environment policy cannot be verified. A recipe installation has no
script to fail closed for you: check eligibility before step 3, and treat an unexpected readback
in step 3 as a stop.

Older GitHub Enterprise Server versions are unsupported pending a separate compatibility review.

## Migrating an existing installation

> Deprecated. This is the migration onto the managed product. A new adoption should install
> [the recipe](#the-hand-installable-recipe) instead, and an existing managed installation should
> follow
> [Converting a managed installation to the recipe](#converting-a-managed-installation-to-the-recipe).

Existing adopters need one local preparation before running `init --github`. Earlier ordinary
`init` guidance produced an unmarked `.github/workflows/doc-lattice.yml` when its printed workflow
was installed. In the same reviewed change, inspect that canonical offline target, then remove it
so `init --github` can install the managed replacement, and inspect and remove any old Linear
workflow occupying `.github/workflows/doc-lattice-linear.yml`. Run `init --github` only after both
canonical targets are absent so the final diff shows the new managed replacements. `ci refresh`
cannot adopt an unmarked file and will fail closed instead of overwriting it.

Canonical target cleanup is only collision handling. Also inventory the repository's workflows and
remove every old hand-written Linear workflow, regardless of path or filename, in the same reviewed
migration change. `ci audit` cannot discover legacy workflow indirection at all, since it inspects
no `run:` commands, so that inventory is a manual review step.

### Rotating the Linear key

For an existing installation, rotate or obtain a Linear key out of band. After the pre-generation
workflow replacement described above, set the replacement key only as
`DOC_LATTICE_LINEAR_API_KEY` on the `doc-lattice-linear` environment, and delete every reported
repository-scoped secret under both the legacy `LINEAR_API_KEY` and dedicated names. Rotation is
preferred because the broader key may already have been exposed. Repository administrators cannot
always inspect organization secret visibility, so obtain organization-owner confirmation that
neither name is exposed to this repository, or have the owner remove or exclude it. Setup is not
complete until bootstrap `verify` and local `ci audit` both pass.

## Installation

> Deprecated. This is the managed installation procedure, and 5.0 removes the commands it uses.
> A new adoption should follow [the recipe](#the-hand-installable-recipe) instead.

Run this human-maintainer sequence from reviewed, trusted project state:

1. Generate and review the local managed artifacts.

   ```bash
   uvx --python 3.13 --from doc-lattice==4.1.0 doc-lattice init \
     --github --repository OWNER/REPO
   ```

2. Establish the reconcile baseline, on an initial adoption only.

   Annotate your documents, then run this once in the same reviewed change, before the generated
   workflows reach `main` and the gates begin running. Commit the annotated input state first and
   run from an otherwise clean working tree, so the reconcile-only diff stays reviewable and
   revertible:

   ```bash
   uvx --python 3.13 --from doc-lattice==4.1.0 doc-lattice reconcile --all
   ```

   An installation migrated per `## Migrating an existing installation` above skips this step.
   When this step applies, what the clean-tree precondition buys you, and what the step does not
   promise are owned by [README.md](README.md#adopting-doc-lattice-in-your-docs-repo); the
   selector semantics are owned by [RECONCILE.md](RECONCILE.md).

3. Inspect the remote repository, plan eligibility, environment, and visible secret names.

   ```bash
   bash .github/doc-lattice-bootstrap.sh plan OWNER/REPO
   ```

4. Apply and read back the exact `main`-only environment policy after typing the canonical
   repository identity.

   ```bash
   bash .github/doc-lattice-bootstrap.sh apply OWNER/REPO
   ```

5. Set the dedicated environment secret separately.

   Stop unless `apply` printed the exact success phrase: `environment policy verified`.

   ```bash
   # Continue only after apply prints: environment policy verified
   gh secret set DOC_LATTICE_LINEAR_API_KEY \
     --env doc-lattice-linear --repo OWNER/REPO
   ```

6. Complete secret migration in the same reviewed change. Run either deletion only when `plan` or
   `apply` reported that repository-scoped name.

   ```bash
   gh secret delete LINEAR_API_KEY --repo OWNER/REPO
   gh secret delete DOC_LATTICE_LINEAR_API_KEY --repo OWNER/REPO
   ```

7. Verify both the remote environment state and the committed local workflow policy.

   ```bash
   bash .github/doc-lattice-bootstrap.sh verify OWNER/REPO
   uvx --python 3.13 --from doc-lattice==4.1.0 doc-lattice ci audit \
     --repository OWNER/REPO
   ```

Every initial and every later `plan`, `apply`, or `verify` execution must use a bootstrap script
from reviewed trusted project state. Its ownership marker is useful installation metadata, not a
substitute for reviewing the executable shell content before running it.

Do not run the secret-setting command until `apply` has re-read and proved the exact `main`-only
environment policy. `apply` never receives the Linear key. `gh secret set` prompts for the value or
reads it from stdin, so the value is not part of the command arguments. This ordering is a
maintainer procedure; the server-side GitHub environment scope is the authorization control.

## Bootstrap script semantics

> Deprecated. The bootstrap script is a managed artifact, and 5.0 removes the commands that
> generate and maintain it. The recipe replaces its readback with the `gh` commands in
> [step 6](#6-verify-by-hand).

Bootstrap `plan` and `verify` exit 0 only when the protected policy, dedicated environment secret,
and repository-secret cleanup are all complete. They exit 1 for coherent but incomplete state and
2 when inspection is unreliable or setup is unsupported. `apply` first prints and fingerprints the
reviewed state, requires an attached stdin TTY and the exact canonical `OWNER/REPO`, then reinspects
before mutation. A first-time `apply` normally exits 1 because the separately entered secret is not
present yet. It exits 0 if setup is already complete and exits 2 on EOF, non-TTY input, confirmation
mismatch, changed state, or another tool error. There is no `--yes`, `--force`, environment
variable, or other noninteractive apply bypass. The same no-bypass rule applies to
`ci refresh --apply`.

GitHub API updates are not transactional, but completed remote state is re-readable and safe
partial setup can be resumed with a fresh `plan` and `apply`. The script does not roll back or
delete preexisting remote state. If an existing environment has broader or ambiguous rules, it
refuses to narrow or claim ownership of that environment and requires manual remediation.

## Auditing and refresh

> Deprecated. `ci audit` and `ci refresh` are removed in 5.0. The recipe replaces them with the
> manual review and readback in [step 6](#6-verify-by-hand), and with whole-block replacement in
> [Upgrading a recipe installation](#upgrading-a-recipe-installation).

### `ci audit`

`ci audit` is meaningful only after `init --github`: before adoption, the absent artifacts
intentionally produce exit-1 findings. The audit checks every workflow for `pull_request_target`
and Linear secret references. Least-privilege permissions, action pins, checkout credentials,
caching, triggers, and exact command structure are scoped to the two canonical managed workflow
paths, so an unrelated release workflow may legitimately use `contents: write`.

Whole-context, wildcard, or computed `secrets` access fails closed unless inspection proves it
selects one static unrelated name. A reusable-workflow job's `secrets: inherit` is whole-context
access because it forwards every available caller secret, so it always produces a
`LINEAR_SECRET_REFERENCE` finding. For the bootstrap script, audit validates only presence and
ownership metadata rather than content equality; the adjacent attributes artifact is checked for
its exact effective LF rule. Local audit also cannot see remote environment or organization-policy
drift, so rerun bootstrap `verify` from reviewed trusted state after relevant policy, visibility,
plan, rename, or transfer changes.

When `ci audit` omits `--repository`, it resolves the local `origin` only from GitHub.com SCP
(`git@github.com:OWNER/REPO.git`), `ssh://git@github.com/OWNER/REPO.git`, or
`https://github.com/OWNER/REPO.git` form, with the `.git` suffix optional. Comparisons are ASCII
case-insensitive, and the repository segment is limited to GitHub's 100-character maximum. The
offline audit cannot establish GitHub's canonical display casing. Bootstrap `plan` and `verify`
read the API `full_name` and require its spelling and case to match the generated literal exactly.
Origin lookup runs from the already-resolved Git top-level, so identity resolution and managed-file
inspection always refer to the same worktree.

### `ci refresh`

A repository name, transfer, or casing change requires:

```bash
doc-lattice ci refresh --repository CANONICAL/NAME
doc-lattice ci refresh --repository CANONICAL/NAME --apply
```

The preview exits 0 when current, 1 after printing an update diff, and 2 for an unreadable,
unmarked, or otherwise unsafe target. The diff renders non-line-ending byte controls as visible
`\xNN` escapes and Unicode format controls as `\uNNNN` or `\UNNNNNNNN` instead of sending
repository-controlled sequences to the terminal. Apply prints the same diff, requires typing the
explicit repository identity exactly, repeats preflight after confirmation, and atomically replaces
only marked canonical artifacts or creates a missing one.

Before publishing a missing artifact, both an initial create and a retry synchronize every
validated ancestor directory entry. Mixed versions after an interruption are safe to preview and
resume. Use this flow for generator upgrades and repository renames, then review and commit the
resulting diff. Refresh moves a managed artifact forward only. When an installed ownership marker
pins a version newer than the one being generated, the preview refuses rather than rewriting the
artifact backward, so running an older doc-lattice against a newer installation cannot silently
downgrade it. GitHub generation and refresh accept only final-version syntax such as `2.0.0`:
this rejects pins that can never resolve as final releases, but it does not prove the release is
already published or that an unreleased source checkout matches that release.

Publication holds a nonblocking advisory lock on the repository root for its whole run, so
`init --github` and `ci refresh --apply` never write over each other. A competing run refuses with
`managed artifact refresh is in progress; retry after it exits` and leaves every managed artifact
unchanged. The guarantee covers the four managed artifacts, not the whole `init --github` run:
that command scaffolds `.doc-lattice.yml` before publication, so a run refused the lock, or
refused for want of locking, can still have created the config. Rerunning after the competing run
exits leaves that config untouched and publishes the artifacts. Publication requires POSIX
advisory locking and refuses on a platform without it; preview, `ci audit`, and every other
read-only path take no lock.

## Security model

This section describes the boundary both setups share. Everything in it is true of a recipe
installation, because the recipe installs the same workflow shape into the same environment. What
differs is only that nothing checks it for you.

The generated environment is the authoritative secret boundary. It allows only the exact `main`
branch, and the dedicated environment-only secret is mapped to `LINEAR_API_KEY` only on the final
step of the trusted workflow. Removing the environment binding removes secret access. Current
ordinary `pull_request`, `pull_request_review`, and `pull_request_review_comment` runs use
`refs/pull/N/merge`, which the environment policy rejects. `pull_request_target` is different: it
uses the default branch ref, so the environment can authorize it while it handles untrusted input.
For that reason audit bans `pull_request_target` repository-wide, the trusted job's own event
allowlist refuses it a second time, and trusted default-branch review remains a load-bearing
control. A recipe installation has only the second of those three, which is why step 2 forbids
adding a pull-request-family trigger and step 6 asks you to check the rest of the repository by
hand. GitHub's
[December 2025 ref-semantics changelog](https://github.blog/changelog/2025-11-07-actions-pull_request_target-and-environment-branch-protections-changes/)
records this behavior change.

Before December 8, 2025, GitHub evaluated environment branch policy for pull-request-family runs
against the attacker-controlled pull-request head branch. The exact `main`, with no pattern, rule
was load-bearing under those semantics: relaxing it to a pattern such as `release/*` would
authorize attacker-chosen matching head branches. Even the exact name could be attacker-chosen, so
this design does not claim that the rule repairs the older behavior.

No generated workflow runs real `reconcile`; the offline workflow does not run even
`reconcile --dry-run` in this release. The exact managed triggers also omit `merge_group`, so merge
queues are unsupported until a generator release adds that event. Both managed workflows disable
persistent cross-run setup-uv and Actions caching; `uv` may still use its ephemeral job-local cache
while one runner job is active. Introducing persistent caching requires a separate security review.
Optional required environment reviewers and disabled administrator bypass can add manual approval
to each Linear run, but they are administered manually outside the initial generated script and
depend on repository visibility and plan support.

The boundary does not protect malicious code already reviewed and admitted to `main`. Other
residual risks include a compromised maintainer workstation or `gh` binary, pinned action, package
artifact, or dependency; a maintainer later broadening the environment; invisible organization
secret policy; and later visibility or billing changes that disable controls. Branch governance,
bootstrap `verify`, local `ci audit`, key rotation, and optional environment review address
different parts of that residual risk rather than replacing the environment boundary. A recipe
installation loses the two of those that were commands, so its residual risk is higher by exactly
that much.

## Why the managed product is being retired

The managed setup had no installations. Maintaining a generator, an offline auditor, a byte-level
refresher, and a bootstrap script for a boundary that a documented recipe reaches directly was not
a trade worth continuing, and the check and lint half of it is already what plain `init`
scaffolds.

The deprecation is documentation and help text only. Invocation stdout, stderr, and exit codes for
`init --github`, `ci audit`, and `ci refresh` are unchanged in 4.x, because a stderr warning
cannot be made compatibility-safe for a script that already parses those channels.
[AD-10](ARCHITECTURE.md#ad-10-output-selector-compatibility-converges-in-20) records that
reasoning and the same documentation-only migration notice it produced before.
[AD-25](ARCHITECTURE.md#ad-25-the-ci-shell-scanner-is-extracted-to-doc-lattice-shell-lint) records
the earlier extraction of the shell scanner out of the same subsystem.

## Out of scope: shell run-body linting

Shell run-body linting is not part of this contract. It was extracted to the standalone
[doc-lattice-shell-lint](https://github.com/Guardantix/doc-lattice-shell-lint) tool, runnable as
`uvx doc-lattice-shell-lint`. `doc-lattice ci audit` performs no shell analysis and reports the
structural workflow findings above only. Anyone who wants that lint adds it to a separate workflow
file of their own, not to a managed workflow: audit compares the full action and command sequences
of the two canonical managed paths, so an added `run:` step there reports `MANAGED_COMMAND` drift
and an added `uses:` step reports `MANAGED_ACTION`. A recipe installation has no managed paths, so
adding that lint to either workflow is simply an edit you own.
[AD-25](ARCHITECTURE.md#ad-25-the-ci-shell-scanner-is-extracted-to-doc-lattice-shell-lint) owns
that extraction.
