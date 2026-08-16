# Managed GitHub and Linear setup

To add protected Linear reporting to a docs repository, a human maintainer generates and reviews
four committed, create-only artifacts: two GitHub Actions workflows, a bootstrap script that
configures the protected GitHub environment, and a scoped `.gitattributes` file. This document
covers what those artifacts are, what the setup requires, how to migrate an existing installation,
the installation procedure, the `ci audit` and `ci refresh` commands that maintain the
installation, and the security model the design rests on.

## What the managed setup installs

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
repository secret metadata, or environment policy cannot be verified.

Older GitHub Enterprise Server versions are unsupported pending a separate compatibility review.

## Migrating an existing installation

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

The generated environment is the authoritative secret boundary. It allows only the exact `main`
branch, and the dedicated environment-only secret is mapped to `LINEAR_API_KEY` only on the final
step of the trusted workflow. Removing the environment binding removes secret access. Current
ordinary `pull_request`, `pull_request_review`, and `pull_request_review_comment` runs use
`refs/pull/N/merge`, which the environment policy rejects. `pull_request_target` is different: it
uses the default branch ref, so the environment can authorize it while it handles untrusted input.
For that reason audit bans `pull_request_target` repository-wide, and trusted default-branch review
remains a load-bearing control. GitHub's
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
different parts of that residual risk rather than replacing the environment boundary.

## Out of scope: shell run-body linting

Shell run-body linting is not part of this contract. It was extracted to the standalone
[doc-lattice-shell-lint](https://github.com/Guardantix/doc-lattice-shell-lint) tool, runnable as
`uvx doc-lattice-shell-lint`. `doc-lattice ci audit` performs no shell analysis and reports the
structural workflow findings above only. Anyone who wants that lint adds it to a separate workflow
file of their own, not to a managed workflow: audit compares the full action and command sequences
of the two canonical managed paths, so an added `run:` step there reports `MANAGED_COMMAND` drift
and an added `uses:` step reports `MANAGED_ACTION`.
[AD-25](ARCHITECTURE.md#ad-25-the-ci-shell-scanner-is-extracted-to-doc-lattice-shell-lint) owns
that extraction.
