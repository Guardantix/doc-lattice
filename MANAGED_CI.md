# Protected Linear reporting in CI

This document owns the hand-installable recipe for running `doc-lattice linear` in CI without
exposing its API key to every workflow in the repository. You add two workflows that you own, and
the protection comes from a GitHub environment whose deployment allow list is exactly `main`,
together with a workflow that maps the dedicated secret only onto its final step.

The recipe replaced a managed product, removed in 5.0, that generated and maintained four
committed, create-only artifacts around the same boundary: two GitHub Actions workflows, a
bootstrap script that configured the protected GitHub environment, and a scoped `.gitattributes`
file. It added drift detection, byte-level refresh, and a scripted remote readback on top of that
boundary. Those additions, not the boundary itself, are what this recipe does without. An
installation left over from 4.1.0 converts with
[Converting a managed installation to the recipe](#converting-a-managed-installation-to-the-recipe),
and [CHANGELOG.md](CHANGELOG.md) carries the release-facing summary of the removal.

The [requirements](#requirements) and the [security model](#security-model) below apply to every
installation.

## The hand-installable recipe

### What you install

- `.doc-lattice.yml`, scaffolded by plain `init`.
- The reconcile-artifact `.gitignore` lines and the pre-commit hooks that plain `init` prints
  alongside the workflow. [README.md](README.md#ordinary-offline-setup) owns what those blocks are
  and where each one goes; this recipe assumes you install them as it describes.
- `.github/workflows/doc-lattice.yml`, the offline check and lint gate that plain `init` prints.
  You own it, and it runs only `check` and `lint`, so it neither requires nor receives
  `LINEAR_API_KEY`.
- `.github/workflows/doc-lattice-linear.yml`, the trusted Linear gate. Plain `init` does not print
  this one, so this document supplies it in full below.
- A `doc-lattice-linear` GitHub environment whose deployment allow list is exactly the `main`
  branch, holding one secret named `DOC_LATTICE_LINEAR_API_KEY`.

The recipe has no bootstrap script, and no `.github/.gitattributes` rule, which existed only to
hold that script at LF line endings after checkout.

Run every step from reviewed, trusted project state, and land the whole setup as one reviewed
change.

Push once, after step 5 and before step 6, and push step 5's own output with it. Steps 1, 2 and 5
only change local state, steps 3 and 4 only change GitHub, and step 6 is the first
step that needs the workflows to exist on `main`, so the single push belongs between them.

Step 5 deliberately stops with its reconcile diff uncommitted, so that diff stays reviewable on its
own, which makes committing it yours to do before you push. Publish the annotated input without it
and you have published the unreconciled state: `check` gates on unreconciled edges exactly as it
does on stale ones, so the offline workflow's first run on `main` reports UNRECONCILED instead of
the baseline this step exists to establish. Review the reconcile-only diff, commit it, then push.

Do not push earlier to watch it work, either. This workflow triggers on every push to `main`, so
the push that lands it is also the gate's first run, and everything that run depends on, the
environment, its branch policy, and its secret, is created by steps 3 and 4.

Push before those and GitHub creates the environment for you, the moment the run starts, because
the job names one. It arrives carrying no deployment branch policy and no protection rules at all,
which is the `NO-BRANCH-POLICY` state step 3's readback calls out as the least protected an
environment can be, under exactly the name this design depends on, and nothing warns you. Step 3's
existence precondition then stops you on an environment you created yourself, so a check written to
catch somebody else's environment has been spent on your own. If that has already happened, inspect
the environment before you continue rather than assuming it is the one your run created: step 3's
create call rewrites the policy, and afterwards nothing can show you what was there before.

That inspection is about provenance, and how protected the environment looks does not carry it. One
created by hand and left with its defaults holds no branch policy and no protection rules either,
so the state your push produces and the state step 3's precondition exists to catch are the same
state. Reading `NO-BRANCH-POLICY` back says nothing about who made it, and taking it for proof of
ownership would hand over the takeover that precondition exists to stop. When it appeared is what
separates them, and the environments endpoint step 3 already reads carries that:

```bash
gh api --hostname github.com \
  "repos/OWNER/REPO/environments/doc-lattice-linear" \
  --jq '.created_at'
```

Created when your push started its run, on a repository you know carried no such environment
before it, it is yours to take over: carry on from step 3 and confirm both readbacks before going
anywhere near the secret. Created earlier, or at a time you cannot account for, it is step 3's stop
exactly as that step writes it, and the environment is somebody else's until you have established
otherwise. Inspect it and decide deliberately rather than running the create call over it.

### 1. Scaffold the config and the offline workflow

```bash
uvx --python 3.13 --from doc-lattice==6.0.0 doc-lattice init --default-branch main
```

Plain `init` writes `.doc-lattice.yml` when it is absent and prints three blocks: the
`.gitignore` lines, the pre-commit hooks, and the offline workflow. Install all three.
Save the printed workflow as `.github/workflows/doc-lattice.yml`, and follow
[README.md](README.md#ordinary-offline-setup) for the other two. The `.gitignore` block is needed
before step 5, which runs `reconcile` and writes the artifacts that block covers.

Installing the pre-commit block means pasting it into `.pre-commit-config.yaml`, which leaves it
inert: no Git hook is written, and nothing runs on commit yet. Enabling the gates is a separate
act, and on an initial adoption it belongs in step 5 rather than here, because activating now
would block the commit that step depends on. A conversion has no such constraint and enables them
immediately. [README.md](README.md#enabling-the-gates) owns both orders and the commands.

Do pass `--default-branch main`, and read the branch back off stderr: the run prints `workflow
triggers on branch main (--default-branch)`. Without the flag `init` takes the trigger branch from
the local `origin/HEAD`, which is cached state, and a target that is stale but still exists in the
clone cannot be told from a current one without the network. The offline workflow would then gate
a branch this repository has moved off, leaving `check` and `lint` absent from `main` while every
readback in step 3 and step 6, which look only at the Linear gate, still comes back exactly as
documented. Naming the branch costs nothing here, because this recipe is pinned to `main`
throughout and step 3 stops outright on a repository whose default branch is anything else.

`--default-branch` reached `init` in 5.0 alongside this recipe, and the pin above carries it. If
the command above ever exits 2 with `No such option: --default-branch`, you are running an
earlier release than the pin names. Do not work around that by dropping the flag: the
stale-`origin/HEAD` hazard the flag exists to close is the reason this step names the branch, and
an installation that silently gates the wrong branch is the outcome this whole recipe is written
to prevent. Read this document at the release you are
installing, and use the `doc-lattice==` pin that copy carries.

That workflow carries the two pinned `uses:` lines the release ships, and the Linear workflow in
the next step carries the same two. Keep this output until step 2 is done and confirm they match:
a difference means this document and the release you are installing have diverged, so stop rather
than reconciling it by hand.

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
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          enable-cache: false
      - name: Install pinned doc-lattice without the Linear secret
        run: |
          uv python install 3.13
          uv venv --python 3.13 "$RUNNER_TEMP/doc-lattice-venv"
          uv pip install --python "$RUNNER_TEMP/doc-lattice-venv/bin/python" doc-lattice==6.0.0
      - name: Run trusted Linear gate
        env:
          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}
        run: '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear --exit-code'
```

Every line of that job is load-bearing, and no part of it is checked for you:

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
- The install step declares no `env:`, so the key is absent from its process environment while
  packages are resolved and installed. That property is scope, not timing: `environment:` binds at
  the job level, so the secret is resolvable from the job's first step, and promoting it to a
  job-level `env:` would place it in every step including this one.
- Both actions are pinned by commit SHA with a trailing version comment, `persist-credentials:
  false` keeps the job token out of `.git/config`, and `enable-cache: false` keeps a cross-run
  cache another workflow could populate out of the gate.
- `permissions: contents: read` is the whole token scope this job needs, and it is declared at the
  workflow level. A job-level `permissions:` block would override it wholesale, so do not add one.
- There is no `continue-on-error:` anywhere in the file, and none belongs there. On the final step
  it would turn every DANGER or BLOCKED finding into a green run, suppressing the one signal this
  gate exists to raise.

### 3. Create the protected environment

Requires an authenticated `gh` and repository owner or administrator authority. Substitute your
canonical `OWNER/REPO` throughout.

Check both preconditions before creating anything:

```bash
gh api --hostname github.com "repos/OWNER/REPO" --jq '.default_branch'

gh api --hostname github.com --paginate \
  "repos/OWNER/REPO/environments" \
  --jq '.environments[].name | select(ascii_downcase == "doc-lattice-linear")'
```

The first must print `main`. This design hard-codes that branch in the workflow trigger, the `if:`
guard, and the deployment allow list, so on any other default branch the gate never runs even
though every readback below still comes back exactly as documented.

The second must print nothing. An environment named `doc-lattice-linear` that already exists is a
stop, not something to run the commands below over: the create call rewrites its deployment branch
policy, and the readback that follows reports only the state that call just wrote, so it can never
show you what the environment was protected by beforehand. Inspect it, decide deliberately whether
it is yours to take over or remove, and only then continue.

The comparison is case-folded, and prints whatever casing the existing environment carries, because
GitHub environment names are not case sensitive. A `Doc-Lattice-Linear` already on the repository is
the same environment as far as the create call below is concerned, so an exact-match precondition
would print nothing and then hand you a silent takeover of somebody else's environment: the one
outcome this check exists to prevent.

Only once both preconditions hold:

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
  --jq '.deployment_branch_policy
        | if . == null then "NO-BRANCH-POLICY"
          else [.protected_branches, .custom_branch_policies] | @tsv end'

gh api --hostname github.com --paginate \
  "repos/OWNER/REPO/environments/doc-lattice-linear/deployment-branch-policies" \
  --jq '.branch_policies[] | [.name, .type] | @tsv'
```

The first command must print `false` then `true`. `NO-BRANCH-POLICY` names the state where the
environment carries no policy at all and every branch may deploy, which is the least protected it
can be; the jq spells it out because an unnamed null would otherwise print as a bare tab. The
second must print exactly one row, `main` then `branch`. Anything else means the policy is not the
one this design depends on: stop, and do not continue to step 4.

### 4. Set the environment secret and remove repository-scoped copies

Only after the readback above is exactly right:

```bash
gh secret set DOC_LATTICE_LINEAR_API_KEY \
  --env doc-lattice-linear --repo github.com/OWNER/REPO
```

`gh secret set` prompts for the value or reads it from stdin, so the key is never part of the
command arguments.

The credential check in [step 6](#6-verify-by-hand) reads your local `LINEAR_API_KEY` rather than
the copy GitHub ends up holding, so run it against the value before this command stores it if your
lattice is already annotated. An initial adoption annotates in step 5, after this one, which is why
step 6 records that gap as the residual rather than closing it.

Every `gh secret` command here carries the `github.com/` host prefix on `--repo`. Unlike `gh api`,
these subcommands take no `--hostname`, so without the prefix they follow whichever host `gh` is
currently authenticated against. That matters most for the deletions below: against the wrong host
they return the same not-found result as a secret that was already absent, which is exactly the
outcome you are told to expect.

A repository-scoped Linear key defeats the whole boundary, because every workflow in the
repository can read it. List what exists first:

```bash
gh secret list --repo github.com/OWNER/REPO
```

Then run a deletion only for a name that listing actually reported, so a not-found result is never
mistaken for a successful cleanup:

```bash
gh secret delete LINEAR_API_KEY --repo github.com/OWNER/REPO
gh secret delete DOC_LATTICE_LINEAR_API_KEY --repo github.com/OWNER/REPO
```

If you are converting an existing installation that used a repository-scoped `LINEAR_API_KEY`,
rotate the key out of band rather than reusing it. The broader key may already have been exposed.
Repository administrators cannot always inspect organization secret visibility, so obtain
organization-owner confirmation that neither name is exposed to this repository, or have the owner
remove or exclude it.

### 5. Establish the reconcile baseline and enable the gates

The baseline is for an initial adoption only. Annotate your documents, then run this once in the
same reviewed change, before the workflows reach `main` and the gates begin running. Commit the
annotated input state first and run from an otherwise clean working tree, so the reconcile-only
diff stays reviewable and revertible:

```bash
uvx --python 3.13 --from doc-lattice==6.0.0 doc-lattice reconcile --all
```

A conversion from an existing installation skips the baseline. When it applies, what the clean-tree
precondition buys you and what the step does not promise are owned by
[README.md](README.md#adopting-doc-lattice-in-your-docs-repo); the selector semantics are owned by
[RECONCILE.md](RECONCILE.md).

Enabling the gates is not initial-adoption-only, and it belongs here rather than in step 1. Step 1
left the pasted pre-commit block inert. Activate it once the baseline is in hand and `check` and
`lint` both come back clean, and before you commit the reconcile diff. A BROKEN edge survives the
baseline and still exits 1, so activating with one outstanding blocks that commit:

```bash
uv tool install pre-commit
uv tool run pre-commit install
```

Committing that diff is then the first gated commit, and both hooks running on it is what shows
the activation took. [README.md](README.md#enabling-the-gates) owns why this pair rather than
`uvx pre-commit install`, what an established installation does instead, and why a commit that
stages no Markdown does not confirm anything.

### 6. Verify by hand

Nothing verifies a recipe installation for you. Run these yourself, and rerun them after any
policy, visibility, plan, rename, or transfer change:

```bash
gh api --hostname github.com --paginate \
  "repos/OWNER/REPO/environments/doc-lattice-linear/secrets" --jq '.secrets[].name'

gh secret list --repo github.com/OWNER/REPO
```

The first must print `DOC_LATTICE_LINEAR_API_KEY` and nothing else that carries a Linear key. The
second must not list `LINEAR_API_KEY` or `DOC_LATTICE_LINEAR_API_KEY` at repository scope.

On an organization-owned repository, run the third read as well:

```bash
gh api --hostname github.com --paginate \
  "repos/OWNER/REPO/actions/organization-secrets" --jq '.secrets[].name'
```

It lists the organization secrets exposed to this repository, and must not carry a Linear key
either: an organization secret is readable by every workflow here and appears nowhere in the
repository-scoped listing, so without this call the check looks clean while the boundary is open.
As in step 4, repository administrators cannot always inspect organization secret visibility, so
obtain organization-owner confirmation rather than treating an empty result as proof.

That endpoint accepts organization-owned repositories only. On a user-owned repository it fails
with `HTTP 422 Validation Failed`, which is not a finding and not a reason to stop: a user-owned
repository has no organization scope, so there is no third scope for a Linear key to hide in and
the check is vacuous rather than skipped. Skip it there and treat the first two reads as the whole
secret-scope check. Then repeat the two policy readbacks from step 3.

Confirm the gate is not merely installed but running. Trigger a run rather than reading whatever
run happens to be newest, because a listing alone certifies the past: the failures this check
exists to catch break the gate without producing a run, so the last green run from before the
break stays at the top of the list and answers for a repository that is now broken. Note the
newest run first, trigger one, then verify the run that appears:

```bash
gh run list --repo github.com/OWNER/REPO \
  --workflow doc-lattice-linear.yml --branch main --event workflow_dispatch \
  --limit 1 --json databaseId

gh workflow run --repo github.com/OWNER/REPO doc-lattice-linear.yml --ref main

gh run list --repo github.com/OWNER/REPO \
  --workflow doc-lattice-linear.yml --branch main --event workflow_dispatch \
  --limit 1 --json databaseId,status,conclusion
```

The third call must report a `databaseId` the first call did not, and `status` `completed`. Until
both hold, the dispatch has not registered or has not finished, so rerun that third call rather
than reading the entry above it. The run identifier is the discriminator on purpose: a rename
leaves `main` at the same commit, so a head SHA cannot separate the run you just triggered from
one that ran before the break. A first call that lists nothing at all is fine, and makes any run
the third one reports the new one.

Both listings filter to `workflow_dispatch` because `--limit 1` bounds how many runs are fetched,
not which one. This workflow also runs on every push to `main`, so without that filter a push
landing between the first two commands puts an unrelated run at the top, and the third call reads
a `databaseId` the first did not report while answering for a dispatch that never registered. The
filter leaves one gap it cannot close, a second manual dispatch of this workflow while yours is in
flight, so run this check when you are the only one dispatching it. That new `databaseId` is
`RUN_ID` below:

```bash
gh run view --repo github.com/OWNER/REPO RUN_ID \
  --json jobs --jq '.jobs[] | [.name, .conclusion] | @tsv'
```

The job's conclusion must be `success`. `gh workflow run` failing outright is itself a result: it
means `main` carries no dispatchable `doc-lattice-linear.yml`, which is a failed installation
reported loudly instead of as a stale green. A `skipped` job is a failed installation, not a passing
one: a job whose `if:` guard is false is skipped, and a run whose only job is skipped concludes
successfully. An `OWNER/REPO` left unsubstituted in the workflow does exactly that, silently, where
the same omission in the `gh` commands above would have failed loudly. So does a repository rename,
which breaks the `github.repository` literal while leaving the environment, its policy, and its
secret untouched, and therefore invisible to every other check in this step.

A `success` conclusion does not by itself prove the secret works, and on a fresh installation it
never does. One property of the tool explains that and everything else in this section, so it is
worth stating once rather than rediscovering per command.

**`linear` reads `LINEAR_API_KEY` only when a collectable identifier survives to the client.** An
identifier is collectable when a node in that run's trigger set carries it under `tickets:`, it is
syntactically well formed, and it is in the configured team wherever `linear_team` is set. With no
identifier at all, the run returns before the client is constructed, never reads the key, and
reports nothing. A malformed or cross-team reference is refused on its own rather than stopping the
run: the identifiers are partitioned, and the run returns before the client only when nothing valid
survives. Reject every reference in the trigger set and the key is never read; leave one valid
identifier beside them and the client is built and the key is read for it. Either way the refused
reference grades BLOCKED without being queried, so it is never silent. Every invocation therefore
raises exactly one question: which nodes land in its trigger set.

The gate's own invocation audits, so its trigger set is the nodes carrying stale-shipped findings,
and step 5 is what empties it. `reconcile --all` acknowledges the current state, so a
just-installed repository has no stale edges and hence no collectable identifier, and this dispatch
passes with the secret absent, misnamed, or wrong. Annotating documents does not change that: the
annotations are read, but only a stale-shipped edge puts one in the trigger set.

So treat the first green as proof the gate is installed and running, and not as proof the secret
resolves. The key is first exercised when a real drift appears, which is also the first time the
gate has anything to report, and a stale-shipped edge with a missing key then fails closed with
`LINEAR_API_KEY is not set` and exit 2. Not every drift does that. One on a document carrying no
identifier at all leaves the trigger set with nothing collectable in it and the stored value
untested, which is the first green's blind spot arriving later rather than a second one. A drift
whose identifiers are all malformed or cross-team leaves the value untested too, but loudly: they
grade BLOCKED without being queried, so `--exit-code` fails that run and names what it refused. One
valid identifier beside them is enough to reach the client, and that run both tests the stored
value and reports the refusal.

What a working key produces then depends on the ticket, and only some of it gates the run.
`--exit-code` exits 1 on a DANGER or BLOCKED finding, and this workflow does not pass
`--warn-exit`, so WARNING does not gate and INFO never does. A ticket in a completed state grades
DANGER, one that did not resolve grades BLOCKED, a started one grades WARNING, an unstarted or
backlog one grades INFO, and a triage, canceled, or duplicate ticket yields no finding at all.
Two of those five report a real drift on a run that still concludes successfully, so read the
reported findings rather than the run conclusion.

Two parts of the credential path are checkable outright, and the third is checkable only in a
narrower sense than it first looks:

- **The secret is named and placed correctly.** Step 6's first read already proves the environment
  holds `DOC_LATTICE_LINEAR_API_KEY` and nothing else carrying a Linear key, and its second proves
  no copy sits at repository scope. The organization scope is the one part you cannot settle alone,
  on the terms the third read is given there.
- **The wiring is right.** Read the workflow. `environment: doc-lattice-linear` on the job is what
  grants access at all, and the secret is mapped to `LINEAR_API_KEY` only in the `env:` of the final
  step.
- **A Linear key can be checked, but the check is about your copy rather than GitHub's.** `--from`
  builds its trigger set the other way: it asks what a change to a node would affect and takes the
  downstream nodes of that impact closure, whether or not anything has drifted. Naming a node
  something derives from therefore populates the trigger set with no drift anywhere in the lattice:

  ```bash
  uvx --python 3.13 --from doc-lattice==6.0.0 doc-lattice linear --from SOME_UPSTREAM_ID
  ```

  On a fully reconciled lattice with no key, the gate's own command reports `no stale-shipped
  findings` and exits 0 while this one exits 2 with `LINEAR_API_KEY is not set`. Only the second
  reached the client, which is what makes it usable at all.

  It is the same property, so at least one node in that closure still has to carry a collectable
  identifier, or the run passes having tested nothing. Three conditions on top of that.

  The value must never appear in a command you type. `export LINEAR_API_KEY=...` is no safer than
  writing the assignment inline on the command itself, because shell history keeps the whole line
  either way, and that is the same reason `gh secret set` prompts rather than accepting an
  argument. Read it from a prompt or a secret manager into the environment instead, and clear it
  once the check is done, since an exported value otherwise reaches every later command in that
  shell.

  Install before you supply it, if you want the separation step 2's install step keeps. `uvx`
  resolves and installs in the same invocation, so whatever the environment holds is held while it
  does. Priming its cache with a keyless run first does not settle that, because `uv tool run`
  installs into an ephemeral environment in the uv cache and nothing holds that cache between two
  invocations, so a prune or an eviction puts the install back inside the keyed run. Build the
  environment yourself instead, with the two commands step 2's install step already runs:

  ```bash
  venv="$(mktemp -d)/venv"
  uv venv --python 3.13 "$venv"
  uv pip install --python "$venv/bin/python" doc-lattice==6.0.0
  ```

  Then supply the key and run `"$venv/bin/doc-lattice" linear --from SOME_UPSTREAM_ID`, which is
  the invocation above against a binary that was installed before the key existed in that shell.

  Read what it reports, not just that it succeeded. Nothing in the query names a workspace: it
  filters on team key and issue number, and the key is what chooses the workspace those are looked
  up in. A key belonging to a different workspace therefore resolves your identifier against a
  same-keyed team there and reports that workspace's issue under your reference. The human output
  will not tell you which happened. It prints your own reference and the state's display name, and
  two workspaces can spell a state the same, so a wrong resolution can render identically to a
  right one.

  Ask for the resolved ticket instead:

  ```bash
  "$venv/bin/doc-lattice" linear --from SOME_UPSTREAM_ID --format json
  ```

  Every finding there carries the ticket as Linear returned it, `url` and `title` included, rather
  than the reference you asked with. The URL is the issue's own address in the workspace that
  answered, so compare it against the issue you know and it settles which workspace the key reached.
  A `null` ticket is not a pass, since the reference was refused or not found, and neither is an
  empty findings list, since a ticket in a state the grading does not cover produces none, as above.

  And run the check against the value before `gh secret set` stores it. It reads your local
  `LINEAR_API_KEY`, so once the secret is stored a pass proves only that the value in your hand
  works, never that GitHub holds the same one.

That last gap is the residual, and this recipe's own ordering leaves it open: step 4 stores the
secret before step 5 annotates the lattice this check needs. A wrong stored value therefore
survives installation silently, and what the first drift reaching the client does with it depends
on how it is wrong. A value Linear rejects, or no value at all, closes loudly, with the error and
exit 2 rather than a pass. A valid key for the wrong workspace need not: by the same absence of a
workspace in the query, it resolves your identifier against a same-keyed team wherever that key
belongs and grades whatever issue it finds, which gates only when that issue's state gates. Read
the reported findings rather than the run conclusion, exactly as for the gate above.

Do not manufacture drift on `main` to force that confirmation earlier. Both outcomes are bad, and
which one you get depends on setup you may not have checked. Step 1's pre-commit block runs
`doc-lattice check`, which exits 1 on a stranded `seen:` hash, so where those hooks are installed
this recipe's own tooling refuses the commit. Where they are not, the commit succeeds: the block is
inert until `pre-commit install` has been run in the repository, and pasting it into
`.pre-commit-config.yaml` does not do that. Then the drift reaches `main`, which is the worse half
of the pair: the offline workflow's own run there fails, so it is reported rather than hidden, but
nothing stopped the commit, and it is blocked before landing only where that workflow is a required
check.

Do not reach for that activation mid-adoption to close the gap, either. `check` exits 1 on
unreconciled edges as well as stale ones, and an initial adoption commits exactly those: step 5
has you commit the annotated input before `reconcile --all` acknowledges it. Activating the hooks
before that baseline lands therefore blocks the commit step 5 depends on. Step 5 activates them at
the one point in the sequence where that constraint has lifted; see
[README.md](README.md#enabling-the-gates) for the commands and the established-installation case.

Planting drift also means planting a `tickets:` reference for it to grade, and an annotation added
for a test outlives the test and misattributes the next real finding on that document.

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
pins, the least-privilege token, the disabled caching, and the rule that neither workflow runs
real `reconcile` are all unchanged. The [security model](#security-model) below describes that
boundary.

It does without the machinery the managed product used to watch that boundary for you:

- **Repository-wide audit.** Nothing checks for `pull_request_target` or for another workflow
  reading a Linear secret. That check is the review discipline described in step 6.
- **Drift detection.** Nothing notices when someone edits the Linear workflow, weakens the `if:`
  guard, promotes the secret to a job-level `env:`, or adds a step after the final one.
- **Byte-level refresh.** No command upgrades the workflow, rewrites it after a repository rename,
  or refuses to move an installation backward. Upgrades are the manual procedure below.
- **Scripted remote readback.** There is no `plan`, `apply`, or `verify` that proves the remote
  environment policy, the environment secret, and the absence of repository-scoped copies in one
  run. The `gh` commands in steps 3 and 6 are the manual equivalent, and running them is on you.
- **Ownership markers.** The two workflows are ordinary repository files with no managed identity,
  no version marker, and no create-only protection.
- **Guarded, resumable setup.** The bootstrap script classified the remote state before touching
  it, refused to narrow or take over an environment it did not create, and named which command to
  rerun after a partial run. Step 3 replaces that with preconditions you check and honor yourself,
  and a half-completed step 3 is yours to read back and finish by hand.

That is a real reduction in assurance, and it is the deliberate trade: the managed product had no
installations to justify maintaining it. The boundary the design actually rests on is the GitHub
environment, and the recipe keeps that intact.

### Upgrading a recipe installation

Nothing updates itself. Read the target release's section in [CHANGELOG.md](CHANGELOG.md) first,
because a release that changes generated output carries a `### Migration` subsection.

Print the target release's blocks and replace yours whole:

```bash
uvx --python 3.13 --from doc-lattice==NEW_VERSION doc-lattice init --print-only --default-branch main
```

`--print-only` writes nothing, so this retrieval cannot touch the config you already have and
carries no precondition about which directory you run it in.
Carry `--default-branch main` here for the same reason step 1 does. An upgrade that omits it is
resolved against whatever `origin/HEAD` the checkout you happen to run it in has cached, so the
same release can print a workflow gating a different branch on two machines, and replacing a
correct file with that one retargets the gate without changing anything you would think to review.

Replace the pre-commit block and `.github/workflows/doc-lattice.yml` with the printed versions
rather than hand-editing their pinned versions. The blocks carry generated structure beyond the
pins, so bumping only the pins silently keeps an outdated shape.

No `init` mode regenerates the Linear workflow, so replace that one from this document at the
target release: open `MANAGED_CI.md` for the release you are moving to and copy its step 2 block
whole, then reapply your repository identity. Do not bump only the `doc-lattice==` pin. For the
same reason the ordinary workflow is replaced whole, this workflow's structure and action pins can
change between releases independently of the version it installs.

### Converting a managed installation to the recipe

An installation generated by the removed product still runs, because its workflows pin the exact
release they were generated from and nothing tells them a later release exists. That is the reason
to convert rather than a reason to wait: it sits on an unsupported release until someone moves it.
[CHANGELOG.md](CHANGELOG.md) states that consequence for the release as a whole; the procedure is
here.

Nothing remote changes. The environment, its `main`-only policy, and
`DOC_LATTICE_LINEAR_API_KEY` should already be exactly what the recipe wants.

Confirm that before you begin, by running the [step 6](#6-verify-by-hand) checks while
`.github/doc-lattice-bootstrap.sh` is still present to explain a disagreement. A managed
installation whose bootstrap `apply` never ran, or ran only partially, has remote state that
premise does not hold for, and step 3 below deletes the one tool left that could diagnose it.

The local files then change ownership from the tool to you, in one reviewed change:

1. Replace `.github/workflows/doc-lattice.yml`. The managed offline workflow invokes `ci audit`,
   which no longer exists, so it cannot be carried forward and pinning it at 5.0 or later fails
   outright. Run plain `init --default-branch main` at the release you are converting to and
   save the workflow it prints over the managed one. The managed workflow you are replacing was
   pinned to `main`, so omitting the flag is what would change the branch it gates.
2. Convert `.github/workflows/doc-lattice-linear.yml` by deleting its four ownership marker
   comment lines, the ones beginning `# doc-lattice-managed:`, `# doc-lattice-artifact:`,
   `# doc-lattice-version:`, and `# doc-lattice-repository:`. What remains is the trusted Linear
   workflow of whichever release generated it. Replace that whole with the step 2 block above and
   reapply your repository identity, rather than keeping the older shape or bumping only its
   `doc-lattice==` pin: a workflow's structure and its action pins move between releases
   independently of the version it installs, which is the same reason step 1 replaces the
   offline workflow whole.
3. Delete `.github/doc-lattice-bootstrap.sh`. Its remote work is already done, and its readback is
   replaced by the `gh` commands in step 6.
4. Convert `.github/.gitattributes`, which is a managed artifact carrying the same four ownership
   marker lines as the workflow, followed by the `doc-lattice-bootstrap.sh text eol=lf` rule.
   Delete the file outright if it holds only those five lines. If your repository added rules of
   its own, delete the rule line and the four marker lines and keep the rest.
5. Adopt the manual review in step 6 in place of the audit and refresh commands this
   installation used to run.
6. Enable the gates if this installation never did. The managed setup printed the same pre-commit
   block as guidance, so it may have been pasted at install time and never activated, in which
   case no local commit has ever been gated. Activate immediately: a conversion has an established
   baseline, so the ordering constraint that applies to a first adoption does not apply here. See
   [README.md](README.md#enabling-the-gates).

Run the [step 6](#6-verify-by-hand) verification once more when the change lands.

## Requirements

The eligibility and authority rules below are GitHub's own. Beyond an authenticated `gh`, the
recipe needs no particular platform or shell: the Bash 3.2 rule that used to stand here belonged
to the managed bootstrap script, and went with it.

GitHub.com repositories whose default branch is exactly `main`, and an authenticated GitHub CLI.
The authenticated maintainer must be a repository owner or administrator with authority to manage
environments and inspect repository secret names. Reading organization-plan metadata can require
organization-owner or equivalent `admin:org` authority; unavailable authority fails closed.

GitHub's [deployment and environment documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
defines environment availability and protection behavior. Public repositories are eligible on
current GitHub plans. Private repositories owned by a user require GitHub Pro; private or internal
organization repositories require GitHub Team or Enterprise.

No script fails closed for you. Step 3 opens with the default-branch and existing-environment
preconditions that stand in for the checks the removed bootstrap script performed; confirm plan
and visibility eligibility yourself before running it, and treat any unexpected readback as a
stop.

Older GitHub Enterprise Server versions are unsupported pending a separate compatibility review.

## Security model

The environment is the authoritative secret boundary. It allows only the exact `main`
branch, and the dedicated environment-only secret is mapped to `LINEAR_API_KEY` only on the final
step of the trusted workflow. Removing the environment binding removes secret access. Current
ordinary `pull_request`, `pull_request_review`, and `pull_request_review_comment` runs use
`refs/pull/N/merge`, which the environment policy rejects. `pull_request_target` is different: it
uses the default branch ref, so the environment can authorize it while it handles untrusted input.
The trusted job's own event allowlist refuses it, and trusted default-branch review remains a
load-bearing control. Nothing scans the rest of the repository for it: the managed product's
repository-wide audit did, and it retired with the product, which is why step 2 forbids adding a
pull-request-family trigger and step 6 asks you to check the rest of the repository by hand.
GitHub's
[December 2025 ref-semantics changelog](https://github.blog/changelog/2025-11-07-actions-pull_request_target-and-environment-branch-protections-changes/)
records this behavior change.

Before December 8, 2025, GitHub evaluated environment branch policy for pull-request-family runs
against the attacker-controlled pull-request head branch. The exact `main`, with no pattern, rule
was load-bearing under those semantics: relaxing it to a pattern such as `release/*` would
authorize attacker-chosen matching head branches. Even the exact name could be attacker-chosen, so
this design does not claim that the rule repairs the older behavior.

Neither workflow runs real `reconcile`; the offline workflow does not run even
`reconcile --dry-run` in this release. Both trigger sets deliberately omit `merge_group`, so merge
queues are unsupported. Adding that event is an edit you own, and it needs the same security
review as any other trigger widening. Both workflows disable persistent cross-run setup-uv and
Actions caching; `uv` may still use its ephemeral job-local cache while one runner job is active.
Introducing persistent caching requires a separate security review. Optional required environment
reviewers and disabled administrator bypass can add manual approval to each Linear run, but they
are administered manually outside this recipe and depend on repository visibility and plan
support.

The boundary does not protect malicious code already reviewed and admitted to `main`. Other
residual risks include a compromised maintainer workstation or `gh` binary, pinned action, package
artifact, or dependency; a maintainer later broadening the environment; invisible organization
secret policy; and later visibility or billing changes that disable controls. Branch governance,
key rotation, the readbacks in step 6, and optional environment review address different parts of
that residual risk rather than replacing the environment boundary. The managed product's bootstrap
verifier and its offline audit covered two further parts of it and are gone, so a recipe
installation carries residual risk higher by exactly that much.

## Why the managed product was retired

[AD-32](ARCHITECTURE.md#ad-32-the-managed-github-ci-product-retires-to-a-documented-recipe) owns
that decision: why the product retired, the alternatives it rejected, and how the retirement was
sequenced.

The consequence for an installation is a version freeze. Generated workflows pin the exact release
that produced them and never learn that a later one exists, so a managed installation kept running
unchanged when 5.0 removed the commands, and goes on running until someone converts it. Pinning it
forward is what fails, because its offline workflow invokes `ci audit`, which no longer exists.

## Out of scope: shell run-body linting

Shell run-body linting is not part of this contract. It was extracted to the standalone
[doc-lattice-shell-lint](https://github.com/Guardantix/doc-lattice-shell-lint) tool, runnable as
`uvx doc-lattice-shell-lint`. doc-lattice itself performs no shell analysis. Anyone who wants that
lint adds it as a step in a workflow file of their own, or accepts that adding it to either recipe
workflow is an edit they own: both are ordinary repository files with no generator for them to
drift from.
[AD-25](ARCHITECTURE.md#ad-25-the-ci-shell-scanner-is-extracted-to-doc-lattice-shell-lint) owns
that extraction.
