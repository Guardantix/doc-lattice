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
| Operate the PyPI project | Yanks a release, manages the Trusted Publisher and project roles | A human PyPI account holding the Owner role on the project |

Two properties of this arrangement matter during an incident:

* **Tag creation is not gated by the environment approval.** The `release` job creates and pushes
  the tag and publishes the GitHub Release with no environment attached; only the later `publish`
  job enters `pypi`. Anyone who can land a version-changing commit on `main` has already made the
  tag and the GitHub Release, whatever happens to the PyPI approval afterwards. Approving the
  environment is the last gate before PyPI, not a review of the release as a whole.
* **The three authorities are one person today.** The repository has a single administrator, the
  `pypi` environment names that same account as its required reviewer with self-review permitted,
  `main` requires passing checks but zero approving reviews, and branch protection does not apply
  to administrators. The environment protection does not bind them either: administrator bypass
  is enabled, so the approval can be skipped outright rather than merely self-granted. There is
  no separation of duties to rely on, and the environment approval is a deliberate pause rather
  than a second pair of eyes. Treat it as one: read what you are about to approve.

The "Accounts and access" section below records where each of these is configured and how to
check its current state.

## Release checklist

1. Bump the version in `src/doc_lattice/__init__.py`, `pyproject.toml`, and every
   released-version pin in `README.md` and `MANAGED_CI.md`, and promote `## [Unreleased]` in `CHANGELOG.md` to
   `## [X.Y.Z] - YYYY-MM-DD` so it becomes the first versioned heading, which is the heading
   the version-sync guard reads.

   Those five are the whole set you edit by hand. `uv.lock` records the same version for the
   local package and picks it up in step 2. `.gx-new-version` is not in either group: it records
   the version of the scaffolding tool that generated the project, it is gitignored, and it is
   never bumped on release. `scripts/check_version_sync.py` does not read it, so nothing catches
   the mistake.
2. Run `uv lock` and commit the refreshed `uv.lock`.
3. Confirm the new changelog section is nonempty. The release job checks this too, before it
   pushes the tag, but failing there costs a release run.
4. Add a `### Migration` subsection to that changelog section when the release changes generated
   output in shape or behavior: the printed pre-commit or workflow snippets, or any of the four
   managed artifacts. The trigger is a semantic change, not the version-pin and ownership-marker
   substitution every release performs, which on its own would make the subsection mandatory
   every time. Name the adopter-visible steps, separated by install kind. The generic upgrade
   procedure covering routine pin bumps is owned by
   [README.md](README.md#upgrading); this checklist owns only the rule that a qualifying release
   must carry the subsection.
5. Run the full verification suite, open a pull request, and wait for every CI check to pass.
6. Merge the pull request to `main`.

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
  retry neither re-uploads existing PyPI files nor fails because they already exist. That is
  the mechanical-failure path only. Never rerun to complete a release you already know is bad;
  see "If a release is bad" below.
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
payload itself is wrong. Rerunning a bad payload publishes it. Never move a release tag, and
never delete or replace published PyPI files outside the narrow escalation described under
"Yanking a published release". If the release source is wrong, fix it and cut the next version,
and see "If a release is bad" below for the procedure that goes with the stage you are in.

## If a release is bad

What to do depends on how far the release got, because the pipeline crosses two one-way doors:
the tag and GitHub Release become immutable in the `release` job, and the PyPI upload becomes
irreversible in the `publish` job. Find your stage first.

| Stage | State | Action |
|-------|-------|--------|
| Before the version bump merges | Nothing exists | Fix the pull request. No release happened. |
| Merged, `release` job running, tag not yet created | Nothing immutable yet | Cancel the run now. Every check that can fail the release runs before the tag, so this is the one stage that costs nothing. Revert on `main`, then confirm no `vX.Y.Z` tag exists before assuming you won the race. |
| Tag pushed, no GitHub Release | Tag is immutable, nothing on PyPI; the `release` job already failed, so no run is waiting and no `pypi` approval is pending | Create the Release by hand with the command under "Defect found after the tag, before PyPI approval", then continue at step 4 there. |
| Tag and GitHub Release exist, `pypi` not yet approved | Tag is immutable, nothing on PyPI. A run may or may not be waiting: if `build-release` failed there is nothing pending to cancel, and the tag and Release are still the problem | Withhold approval, cancel any waiting run, record the withdrawal on the Release, then cut the next version. Full procedure under "Defect found after the tag, before PyPI approval". |
| `publish` interrupted, some files on PyPI | Partially uploaded, and what landed is permanent | A payload you still trust is completed by rerunning, since `skip-existing` makes that safe. A payload you do not trust is never completed: treat it as published and go to the yank decision below. |
| Published, non-security defect | On PyPI, installable | Hotfix as the next version. Yank as well only if the release is worse than nothing; "Yanking a published release" gives the threshold. |
| Published, security vulnerability | On PyPI, installable | Draft the advisory privately, release the fix, yank if there is no safe way to use the affected version, then publish the advisory with affected and fixed ranges. "Security vulnerabilities" gives the ordering and why it matters. |

There is deliberately no row for a published release whose GitHub Release failed. The `release`
job publishes the Release before `build-release` and `publish` run at all, so nothing can reach
PyPI without it.

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
with `uvx --from git+...@vX.Y.Z`. If you arrived here by the tag-only path below this does not
apply, because creating that Release was itself a publication and it already reached
subscribers. Otherwise the window is tolerable when the fix follows promptly and nothing is on
PyPI. It is not tolerable if you know someone is already installing from that git ref, or if the
defect is dangerous rather than merely wrong. In either case, do not wait for the
next release to carry the news: tell the affected adopters directly, and open a GitHub issue
naming the withdrawn version so the withdrawal is searchable from outside the Releases page.

`Create and push the tag` and `Publish release notes` are separate steps, so a run that dies
between them leaves the tag with no Release and step 3 with nothing to edit. Do not rerun the
workflow to produce one: rerunning replays a payload you already know is bad and walks it back
up to the `pypi` approval. Create the Release directly instead, which is very nearly the call the
workflow would have made:

```bash
gh release create vX.Y.Z --title vX.Y.Z --verify-tag --latest=false \
  --notes 'Withdrawn before publication. Not on PyPI. Superseded by vX.Y.Z+1.'
```

`--latest=false` is the one behavioral difference from the call the workflow makes, which passes
`--notes-file release-notes.md` where this one takes `--notes` inline. Left off, `gh` infers
the Latest release from tag date and version, so the withdrawn version would take the repository
landing page's **Latest release** slot and the `/releases/latest` API with it, advertising the
version nobody should install until the fix ships.

This is safe to run by hand only while no workflow triggers on a release. Confirm that before
running it, rather than trusting this paragraph to have aged well:

```bash
uv run python - <<'PY'
import pathlib
from ruamel.yaml import YAML

yaml = YAML(typ="safe")
for path in sorted(pathlib.Path(".github/workflows").glob("*.y*ml")):
    on = yaml.load(path.read_text())["on"]
    events = [on] if isinstance(on, str) else list(on)
    print(f"{path.name}:", *events)
PY
```

It prints one line per workflow holding that workflow's complete event list, and none of them
may contain `release`. Parse rather than grep, for two reasons. Grepping for `release:` alone
matches `ci.yml`'s job of that name, since a job key and a trigger key sit at the same
indentation. And grepping `on:` with a fixed window of trailing context, such as `-A15`, prints
only that many lines and silently truncates any longer trigger block, which is the drift this
check exists to catch. The parse has no such window, and it raises rather than printing a
reassuring nothing if a workflow ever stops having a readable `on:` key.

Today `ci.yml` triggers on a push to `main` and on pull requests, and `claude.yml` on issue and
review events, so publishing a Release starts no CI run and advances nothing toward PyPI. It
does reach Releases subscribers, which is the one upside of arriving at this stage by the
tag-only path: the withdrawal notice goes out as a publication rather than as a silent edit.
Then continue at step 4.

### Yanking a published release

Yank when installers resolving the range should skip this version: it is broken, it is
incompatible in a way the version number does not advertise, or it carries a vulnerability with
no safe usage. Prefer a hotfix when the release merely regresses something, since a fix that
installs beats a version that vanishes.

The publish job is upload-only OIDC with no stored PyPI token, so there is no yank command in
this repository and none is added by running the pipeline. A yank is a human action in the PyPI
web UI, performed by a project Owner:

1. Go to <https://pypi.org/manage/project/doc-lattice/releases/>. This requires signing in with
   a PyPI account holding the Owner role. Trusted Publishing covers uploads only and grants
   nobody the ability to yank, and neither does the Maintainer role, which carries upload alone.
2. Find the release, open its **Options** menu, and choose **Yank**.
3. Provide a reason in the confirmation dialog. It is optional and you should always give one:
   PyPI shows it on the release page and serves it through the index API, so it is what an
   adopter sees when their resolver skips the version. Keep it one line, factual, and pointing
   forward, for example: `Broken reconcile rollback; upgrade to 4.1.2.` If you are yanking
   before the fix exists, describe the defect and say a fix is coming rather than inventing a
   version number, and remember the reason is public the moment you save it.
4. Verify it landed:

   ```bash
   curl -sf https://pypi.org/pypi/doc-lattice/X.Y.Z/json | python3 -c \
     'import json,sys; d=json.load(sys.stdin)["info"]; print(d["yanked"], repr(d["yanked_reason"]))'
   ```

   It must print `True` and your reason. The release page also shows a yanked banner. Read
   `info`, which carries the release-level state a yank sets, and pass curl `-f` so a 404 fails
   outright instead of piping an error document into a confusing traceback.

5. Announce it, per the section below.

**A yank does not protect exact-pinned adopters.** PEP 592 requires an installer to ignore a
yanked release whenever the constraint can be satisfied by a non-yanked version. Past that point
the standard stops requiring anything: when the constraint cannot be satisfied without the yanked
release, PEP 592 only says an installer *may* refuse it, and the familiar exceptions, an explicit
non-wildcard `==X.Y.Z` or `===X.Y.Z` pin and a version already recorded in a lock file, are two
suggested approaches it leaves to each installer rather than a boundary it enforces.

In practice the tooling this project targets does refuse. pip has rejected a yanked release whose
range has no other match since 22.0, and uv rejects it today, so an adopter on `>=4.1,<4.2` is
protected even when the yanked release is the only version that range currently matches. Treat
that as installer behavior worth re-checking against the resolver an adopter actually runs, not
as a guarantee the standard gives you: an adopter on pip older than 22.0 is not protected.

Everyone holding an exact pin or a lock entry keeps installing it, silently and indefinitely. A
yank is a signal to resolvers, not a recall. If the defect is serious enough that those adopters
must not keep running it, the yank is not sufficient on its own and the announcement has to reach
them directly.

Never delete a published release or replace a published file. PyPI does not allow reuploading a
filename, so a deletion strands the version permanently rather than fixing it.

The one exception is an artifact that must stop being downloadable at all, such as a
distribution carrying malware or a live credential. A yank does not achieve that, since exact
pins and lock entries still resolve it. Escalate rather than acting alone: mail
<security@pypi.org>, and use **Report project as malware** at the bottom of the project page
sidebar when that is what it is. Do not describe the problem in a public issue or discussion on
the way. Deletion burns the filename permanently, so it is a last resort for an artifact that is
dangerous rather than merely wrong, never a tool for an ordinary regression.

### Security vulnerabilities

A vulnerability runs in this order: draft the advisory privately from the repository's
**Security** tab, release the fix, yank the affected version if there is no safe way to use it,
then publish the advisory with affected and fixed version ranges, so Dependabot and the GitHub
Advisory Database can alert adopters who never read a release note.

The order is the point. Publishing before a fixed version exists tells attackers where to look
while leaving adopters nothing to upgrade to, and the advisory cannot name a fixed range that
does not exist yet. Hold the draft until the fix ships unless the vulnerability is already
public, in which case publishing what adopters can do to protect themselves beats silence.

The yank sits inside that sequence rather than ahead of it, because a yank is itself a
disclosure: PyPI serves the reason through the index API the moment you save it. If you have to
yank before the advisory publishes, write a reason that names the fixed version and says nothing
about the defect. [SECURITY.md](SECURITY.md) owns how a vulnerability is reported and what
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
  needs a line saying so. That edit reaches no already-published Release: notes are extracted
  from the changelog once, at release time, so the Release itself has to be edited separately.

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
new one with the new values, which requires a human account holding the Owner role. Plan the
re-registration as part of any such move, not as cleanup afterwards, and do not assume it is
always self-service. Re-registering a publisher whose values were registered before can be
rejected as already registered and need PyPI admin intervention to clear, which is a support
round trip you do not want to discover mid-migration.

Trusted Publishing does not cover anything but uploads. Yanking, managing project roles, and
re-registering the publisher all need a human PyPI account holding the Owner role, and holding
any role is not the same thing. PyPI's two project roles are not graded versions of each other:
an Owner can do all three, while a Maintainer can only upload, so a Maintainer cannot yank a bad
release, cannot promote a replacement operator, and cannot re-register the publisher after a
rename or transfer.

Record, outside this repository, who the primary and backup operators are, the exact role each
holds (read it at <https://pypi.org/manage/project/doc-lattice/collaboration/> rather than
assuming), and where each account's 2FA recovery codes are kept. The backup has to be an Owner.
A backup Maintainer satisfies a "someone else has a role" check while leaving nobody able to
act, which is the worse failure of the two because it reads as covered. A single Owner with no
backup Owner and no recovery-code custody is an incident away from being unable to yank a bad
release, and this is the point of the release procedure where that becomes visible.

### GitHub repository controls

| Control | Where |
|---------|-------|
| `main` branch protection | Settings, Rules or Branches |
| `pypi` environment reviewers and branch policy | Settings, Environments, `pypi` |
| Private vulnerability reporting | Settings, Code security |
| Administrator access | Settings, Collaborators and teams |

Verify the release-relevant ones from the command line. Project down to the values the claims in
"Who can release" rest on, not merely to the presence of a rule: an environment keeps its
`required_reviewers` rule after the reviewer it names stops being the right account, and a branch
can keep its protection entry after the required checks are gone.

```bash
gh api repos/Guardantix/doc-lattice/environments/pypi --jq \
  '{can_admins_bypass, branch_policy: .deployment_branch_policy,
    rules: [.protection_rules[] | {type, prevent_self_review,
                                   reviewers: [.reviewers[]?.reviewer.login]}]}'
gh api repos/Guardantix/doc-lattice/environments/pypi/deployment-branch-policies \
  --jq '[.branch_policies[] | {name, type}]'
gh api repos/Guardantix/doc-lattice/branches/main/protection --jq \
  '{admins: .enforce_admins.enabled, strict: .required_status_checks.strict,
    checks: .required_status_checks.contexts,
    reviews: .required_pull_request_reviews.required_approving_review_count}'
gh api repos/Guardantix/doc-lattice/private-vulnerability-reporting --jq .enabled
```

The environment response reports only that custom branch policies are in use, which is why the
allowed branches take their own query. That query keeps `type` because the name alone does not
identify a branch policy: a *tag* policy named `main` prints the same name and authorizes
nothing. The repository's own bootstrap verifier reads the same pair and accepts only
`main` with type `branch`, so this check matches the one the tooling already enforces.

Read every answer against what this document claims rather than against the absence of an error:

| Answer | Expected today |
|--------|----------------|
| `rules` | A `required_reviewers` entry naming the one administrator, `prevent_self_review` false. A second `branch_policy` entry with no reviewers is normal and carries nothing |
| `branch_policy` | `custom_branch_policies` true, `protected_branches` false, and the policy list exactly one entry, name `main` and type `branch` |
| `can_admins_bypass` | `true`, the administrator bypass described under "Who can release" |
| `checks` and `strict` | `strict` true, and `checks` exactly `Security scan`, `Tests (3.13)`, `Tests (3.14)`, `Code quality (3.13)`, `Code quality (3.14)` |
| `reviews` and `admins` | `0` and `false`, the state described under "Who can release" |

The `checks` row names its contexts because "every required check is present" is not a testable
claim: any non-empty list satisfies it, including one a dropped context has already shortened.
Compare the names. Note that `yaml-compatibility` gates the `release` job but is not a required
check, so it can fail without blocking a merge.

`can_admins_bypass`, `reviews`, and `admins` are the rows whose expected values are the weaker
settings. They record what is true today, not what is desirable. If any of them ever reads the
stricter value the protection has been tightened, and the authority description above is what
needs correcting rather than the setting.

Private vulnerability reporting must report `true`. The button is a repository setting separate
from [SECURITY.md](SECURITY.md); the file alone does not create the channel. Enabling it is also
not enough on its own, since a report nobody is notified about is a report nobody reads: at
least one administrator or security manager must watch the repository for security alerts.

On a transfer, secrets travel with the repository. That is the trap: they keep working under the
new owner without any decision being made about whether they should. Audit and rotate them
deliberately as part of the move, alongside re-registering the PyPI publisher. Do not assume the
protections came across intact either, since a change of owner or plan can leave `main`
unprotected or an environment rule unenforced without saying so. Rerun the queries above after any
transfer and read the answers against that table.

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
