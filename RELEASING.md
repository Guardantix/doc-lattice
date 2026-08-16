# Releasing doc-lattice

doc-lattice publishes immutable `vX.Y.Z` tags, GitHub Releases, wheels, and source
distributions. PyPI Trusted Publishing trusts the `Guardantix/doc-lattice` repository, the
`ci.yml` workflow, and the `pypi` environment; no PyPI API token is stored.

## Who can release

"Who can release" is three separate authorities on two control planes, and they do not gate each
other. Answering it as one role is how a bad version reaches PyPI.

| Authority | What it can do | Where it is enforced |
|-----------|----------------|----------------------|
| Land a version bump on `main` | Creates the immutable `vX.Y.Z` tag and its GitHub Release | GitHub branch protection on `main` |
| Approve the `pypi` environment | Releases the built distributions to PyPI | GitHub environment protection |
| Operate the PyPI project | Yanks a release, manages the Trusted Publisher and project roles | A human PyPI account with a role on the project |

Two properties of this arrangement matter during an incident:

* **Tag creation is not gated by the environment approval.** The `release` job creates and pushes
  the tag and publishes the GitHub Release with no environment attached; only the later `publish`
  job enters `pypi`. Anyone who can land a version-changing commit on `main` has already made the
  tag and the GitHub Release, whatever happens to the PyPI approval afterwards. Approving the
  environment is the last gate before PyPI, not a review of the release as a whole.
* **The three authorities are one person today.** The repository has a single administrator, the
  `pypi` environment names that same account as its required reviewer with self-review permitted,
  `main` requires passing checks but zero approving reviews, and branch protection does not apply
  to administrators. There is no separation of duties to rely on, and the environment approval is
  a deliberate pause rather than a second pair of eyes. Treat it as one: read what you are about
  to approve.

The "Accounts and access" section below records where each of these is configured and how to
check its current state.

## Release checklist

1. Bump the version in `src/doc_lattice/__init__.py`, `pyproject.toml`, and every
   released-version pin in `README.md` and `MANAGED_CI.md`, and promote `## [Unreleased]` in `CHANGELOG.md` to
   `## [X.Y.Z] - YYYY-MM-DD` so it becomes the first versioned heading, which is the heading
   the version-sync guard reads.

   Those five files are the whole set. `.gx-new-version` is not one of them: it records the
   version of the scaffolding tool that generated the project, it is gitignored, and it is never
   bumped on release. `scripts/check_version_sync.py` does not read it, so nothing catches the
   mistake.
2. Run `uv lock` and commit the refreshed `uv.lock`.
3. Confirm the new changelog section is nonempty. The release job checks this too, before it
   pushes the tag, but failing there costs a release run.
4. Run the full verification suite, open a pull request, and wait for every CI check to pass.
5. Merge the pull request to `main`.

The release pipeline then runs in this order:

1. The `release` job re-asserts version sync, extracts the `## [X.Y.Z]` changelog section into
   the release notes, and smoke-tests the release source. Only then does it create the immutable
   `vX.Y.Z` tag and its GitHub Release, from the notes already extracted. Every check that can
   fail the release runs before the tag exists, so a missing or empty changelog section fails the
   run while there is still nothing immutable to strand.
2. The dependent, unprivileged `build-release` job checks out that exact tag, builds the wheel
   and source distribution, validates both with Twine, and uploads them as an artifact.
3. The `publish` job downloads the validated artifact and uploads it to PyPI through OIDC. It
   does not check out the repository or execute package build code.

After publication, confirm the public index serves the release:

```bash
uvx doc-lattice@latest --version
```

The command must print the released version. The explicit `@latest` request is required: when a
machine carries an installed `uv tool` copy of doc-lattice, an unpinned `uvx doc-lattice`
invocation runs that installed environment without consulting the index, and `--refresh` does
not change this, so an unpinned check can report a stale version against a published release.
After verifying, refresh any installed tool with `uv tool install doc-lattice@latest`.

## Release semantics and recovery

- An ordinary merge that leaves the version unchanged is a no-op.
- Rerunning the workflow for the commit already referenced by the matching tag resumes any
  missing GitHub Release or PyPI publication steps. Publication uses `skip-existing`, so a
  retry neither re-uploads existing PyPI files nor fails because they already exist.
- A commit with an unchanged version whose tag points to an older commit is a no-op.
- A matching tag that points to a source with a different version fails the release.
- When the tag is absent, a push whose pre-push source declares a different version may create it.
  The version bump may appear anywhere in a multi-commit push; the tag identifies the final landed
  commit. A missing version file in the pre-push source can identify the package introduction.
- Malformed current, pre-push, or tagged version declarations fail closed. Unexpected Git or
  source-reading failures also fail closed; they are never treated as permission to publish.

If a release step fails *mechanically*, rerun the same workflow: a runner outage, a network
timeout, an expired cache. Rerunning replays the pipeline against the same commit and resumes
whatever did not finish.

Do not rerun, and do not approve the `pypi` environment, when the failure told you the release
payload itself is wrong. Rerunning a bad payload publishes it. Never move a release tag or
delete or replace files already published to PyPI. If the release source is wrong, fix it and
cut the next version, and see "If a release is bad" below for the procedure that goes with the
stage you are in.

## If a release is bad

What to do depends on how far the release got, because the pipeline crosses two one-way doors:
the tag and GitHub Release become immutable in the `release` job, and the PyPI upload becomes
irreversible in the `publish` job. Find your stage first.

| Stage | State | Action |
|-------|-------|--------|
| Before the version bump merges | Nothing exists | Fix the pull request. No release happened. |
| Tag pushed, no GitHub Release | Tag is immutable, nothing on PyPI, no announcement surface | Create the Release by hand to carry the withdrawal notice, then treat it as the row below. |
| Tag and GitHub Release exist, `pypi` not yet approved | Tag is immutable, nothing on PyPI | Withhold approval. Cancel the run. Cut the next version. |
| Published, non-security regression | On PyPI, installable | Hotfix as the next version. Yank only if the release is broken enough to be worse than nothing. |
| Published, broken or incompatible enough to mislead installers | On PyPI, installable | Yank, then release the fix. |
| Published, security vulnerability | On PyPI, installable | Publish a GHSA with affected and fixed ranges, release the fix, and yank if there is no safe way to use the affected version. |

### Defect found after the tag, before PyPI approval

This is the stage the recovery rules above warn about. The tag and GitHub Release already exist
and cannot be withdrawn, but nothing has reached PyPI yet, so the version is still recoverable
as a version nobody can install.

1. Do not approve the `pypi` environment, and cancel the workflow run so a later approval cannot
   release it by accident.
2. Leave the tag alone. Moving or deleting a release tag breaks every ref already pointing at
   it, and `uvx --from git+...@vX.Y.Z` installs resolve against it.
3. Edit the GitHub Release to say the version was withdrawn before publication and name the
   version that supersedes it. This is a durable record for anyone who reaches the tag, not an
   announcement: GitHub's release notifications fire when a release is *published*, and an edit
   to an already-published one is not documented to notify anybody. Treat the edit as silent.
4. Fix the defect and cut the next version. That version number is now burned; do not reuse it.
   Name the withdrawn version in the new release's notes. Publishing that release is the event
   that actually reaches subscribers, so it is where the withdrawal gets announced.

Between steps 3 and 4 the withdrawal is recorded but unannounced, and the tag stays installable
with `uvx --from git+...@vX.Y.Z`. That window is tolerable when the fix follows promptly and
nothing is on PyPI. It is not tolerable if you know someone is already installing from that git
ref, or if the defect is dangerous rather than merely wrong. In either case, do not wait for the
next release to carry the news: tell the affected adopters directly, and open a GitHub issue
naming the withdrawn version so the withdrawal is searchable from outside the Releases page.

`Create and push the tag` and `Publish release notes` are separate steps, so a run that dies
between them leaves the tag with no Release and step 3 with nothing to edit. Do not rerun the
workflow to produce one: rerunning replays a payload you already know is bad and walks it back
up to the `pypi` approval. Create the Release directly instead, which is the same call the
workflow would have made:

```bash
gh release create vX.Y.Z --title vX.Y.Z --verify-tag \
  --notes 'Withdrawn before publication. Not on PyPI. Superseded by vX.Y.Z+1.'
```

This is safe to run by hand because the workflow triggers only on a push to `main` and on pull
requests, so publishing a Release starts no CI run and advances nothing toward PyPI. It does
reach Releases subscribers, which is the one upside of arriving at this stage by the tag-only
path: the withdrawal notice goes out as a publication rather than as a silent edit. Then
continue at step 4.

### Yanking a published release

Yank when installers resolving the range should skip this version: it is broken, it is
incompatible in a way the version number does not advertise, or it carries a vulnerability with
no safe usage. Prefer a hotfix when the release merely regresses something, since a fix that
installs beats a version that vanishes.

The publish job is upload-only OIDC with no stored PyPI token, so there is no yank command in
this repository and none is added by running the pipeline. A yank is a human action in the PyPI
web UI, performed by a person with a role on the project:

1. Go to <https://pypi.org/manage/project/doc-lattice/releases/>. This requires signing in with
   a PyPI account that holds a project role; Trusted Publishing covers uploads only and grants
   nobody the ability to yank.
2. Find the release, open its **Options** menu, and choose **Yank**.
3. Provide a reason in the confirmation dialog. It is optional and you should always give one:
   PyPI shows it on the release page and serves it through the index API, so it is what an
   adopter sees when their resolver skips the version. Keep it one line, factual, and pointing
   forward, for example: `Broken reconcile rollback; upgrade to 4.1.2.`
4. Verify it landed:

   ```bash
   curl -s https://pypi.org/pypi/doc-lattice/X.Y.Z/json | python3 -c \
     'import json,sys; d=json.load(sys.stdin)["urls"][0]; print(d["yanked"], repr(d.get("yanked_reason")))'
   ```

   It must print `True` and your reason. The release page also shows a yanked banner.

5. Announce it, per the section below.

**A yank does not protect exact-pinned adopters.** PyPI's rule is that a yanked release is
ignored by installers *unless it is the only release matching the version specifier*. An adopter
pinned with `==X.Y.Z` or `===X.Y.Z` on the yanked version keeps installing it, silently and
indefinitely. A yank is a signal to resolvers, not a recall. If the defect is serious enough
that exact-pinned adopters must not keep running it, the yank is not sufficient on its own and
the announcement has to reach them directly.

Never delete a published release or replace a published file. PyPI does not allow reuploading a
filename, so a deletion strands the version permanently rather than fixing it.

### Security vulnerabilities

A vulnerability follows the yank decision above, plus an advisory. Publish a GitHub Security
Advisory from the repository's **Security** tab with affected and fixed version ranges, so
Dependabot and the GitHub Advisory Database can alert adopters who never read a release note.
Release the fix first when the schedule allows, so the advisory names a version people can
already upgrade to. [SECURITY.md](SECURITY.md) owns how a vulnerability is reported and what
disclosure timeline reporters are asked to follow.

### Where adopters watch for announcements

* **GitHub Releases**, which the pipeline already publishes for every version, is the channel for
  non-security announcements, but only on publication. Subscribers are notified when a release
  is published; editing an already-published one is not documented to notify anyone. So editing
  the bad version's Release records the problem, and describing it in the *fix's* release notes
  is what announces it. Never rely on the edit alone to reach anybody.
* **GitHub Security Advisories** carry security announcements and feed Dependabot.
* **The PyPI yank reason** reaches whoever runs an install that skips the version.
* **[CHANGELOG.md](CHANGELOG.md)** carries the durable record. A yanked or withdrawn version
  needs a line saying so, since the release notes come from it.

Adopters can subscribe to Releases and security alerts specifically through the repository's
**Watch** menu, under **Custom**. That is worth telling them once external adoption starts,
since watching everything is what makes people stop watching.

## Accounts and access

Every control below has an owner, a place its settings live, a behavior on rename or transfer,
and a recovery procedure. Nothing here records a credential value, a recovery code, or a token.

### PyPI project and Trusted Publisher

The `doc-lattice` PyPI project publishes through Trusted Publishing, with no stored API token.
The binding matches four values exactly:

| Field | Value |
|-------|-------|
| Repository owner | `Guardantix` |
| Repository name | `doc-lattice` |
| Workflow filename | `ci.yml` |
| Environment | `pypi` |

**Change any one of those and publication stops.** Renaming the repository, transferring it to
another owner, renaming `ci.yml`, or renaming the `pypi` environment all break the match, and
the failure appears at upload time as a rejected OIDC claim rather than as a warning beforehand.
There is no way to edit a binding: the fix is to delete the stale publisher on PyPI and add a
new one with the new values, which requires a human account with a project role. Plan the
re-registration as part of any such move, not as cleanup afterwards.

Trusted Publishing does not cover anything but uploads. Yanking, managing project roles, and
re-registering the publisher all need a human PyPI account with a role on the project. Record,
outside this repository, who the primary and backup operators are, which role each holds
(verify it at <https://pypi.org/manage/project/doc-lattice/collaboration/> rather than assuming),
and where the account's 2FA recovery codes are kept. A single operator with no backup and no
recovery-code custody is an incident away from being unable to yank a bad release, and this is
the point of the release procedure where that becomes visible.

### GitHub repository controls

| Control | Where |
|---------|-------|
| `main` branch protection | Settings, Rules or Branches |
| `pypi` environment reviewers and branch policy | Settings, Environments, `pypi` |
| Private vulnerability reporting | Settings, Code security |
| Administrator access | Settings, Collaborators and teams |

Verify the release-relevant ones from the command line:

```bash
gh api repos/Guardantix/doc-lattice/environments/pypi \
  --jq '{can_admins_bypass, rules: [.protection_rules[].type]}'
gh api repos/Guardantix/doc-lattice/branches/main/protection \
  --jq '{admins: .enforce_admins.enabled, reviews: .required_pull_request_reviews.required_approving_review_count}'
gh api repos/Guardantix/doc-lattice/private-vulnerability-reporting --jq .enabled
```

Private vulnerability reporting must report `true`. The button is a repository setting separate
from [SECURITY.md](SECURITY.md); the file alone does not create the channel. Enabling it is also
not enough on its own, since a report nobody is notified about is a report nobody reads: at
least one administrator or security manager must watch the repository for security alerts.

On a transfer, branch protection and environment configuration travel with the repository, and
so do its secrets. That is the trap: the secrets keep working under the new owner without any
decision being made about whether they should. Audit and rotate them deliberately as part of the
move, alongside re-registering the PyPI publisher.

### `CLAUDE_CODE_OAUTH_TOKEN`

A repository Actions secret, consumed only by `.github/workflows/claude.yml`. It is a Claude
Code OAuth token belonging to a maintainer's Anthropic account, so the upstream credential owner
is that account, not the repository.

To rotate it: revoke and reissue the token from the owning Anthropic account, update the secret
at Settings, Secrets and variables, Actions, then confirm the workflow still authenticates on
its next run rather than assuming the update took. `gh secret list --repo Guardantix/doc-lattice`
shows the update timestamp, which is the cheapest confirmation that the write landed.

Rotate it on any of: a suspected leak or an accidental log exposure, the owning maintainer
leaving the project, a repository transfer, or the underlying Anthropic account changing hands.
The value never belongs in this repository, in an issue, or in a workflow log.

## Local verification

Run the full verification set from [CLAUDE.md](CLAUDE.md), adding `--locked` to every `uv run`,
then:

```bash
uv run --locked --group dev ruff check scripts/release_gate.py
uv run --locked --group dev ruff format --check scripts/release_gate.py
uv run --locked --group dev ty check scripts/release_gate.py
```

Build and validate exactly the expected artifacts, then smoke-test the wheel in a fresh Python
3.13 environment:

```bash
set -euo pipefail

dist_dir="$(mktemp -d)"
version="$(uv run --locked python -c 'from doc_lattice import __version__; print(__version__)')"
sdist="${dist_dir}/doc_lattice-${version}.tar.gz"
wheel="${dist_dir}/doc_lattice-${version}-py3-none-any.whl"
uv build --out-dir "${dist_dir}"
test -f "${sdist}"
test -f "${wheel}"
artifact_count="$(find "${dist_dir}" -maxdepth 1 -type f ! -name .gitignore | wc -l)"
test "${artifact_count}" -eq 2
uvx --from twine twine check "${sdist}" "${wheel}"

venv_dir="$(mktemp -d)/.venv"
uv venv --python 3.13 "${venv_dir}"
uv pip install --python "${venv_dir}/bin/python" "${wheel}"
"${venv_dir}/bin/doc-lattice" --version
```
