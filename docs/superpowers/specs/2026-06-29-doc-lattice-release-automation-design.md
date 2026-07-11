# doc-lattice Release Automation Slice: Design Spec

**Date:** 2026-06-29
**Status:** Design (post brainstorm). Ready for implementation planning.
**Scope:** Release tooling. A version-consistency guard that blocks drift at PR time, and a
merge-triggered CI job that creates and verifies the `vX.Y.Z` tag so the tag is a product of a green
pipeline rather than a manual step. No change to any engine command; the only code added to the
package is a pure version-consistency function. No network at runtime, no secrets, no LLM.
**Builds on:** `docs/superpowers/specs/2026-06-28-doc-lattice-init-design.md` (the init scaffolding
that pins adopters' codegen to `v{__version__}`, and the release model this slice automates) and
`RELEASING.md` (the manual checklist this slice replaces in part).

This spec turns the deferred "Release-tag automation" item from the roadmap and the init deferral map
(init spec section 11, "Automating and CI-verifying the release tag: deferred follow-up, a
project-wide release-tooling concern") into a buildable design. It does not change how a version is
chosen or what a release contains; it removes the two ways a release currently goes half-done.

## 1. Scope

In scope:

- A pure version-consistency core (`version_check.py`) that, given the three version-bearing texts,
  returns the set of mismatches between `__version__`, the `pyproject.toml` version, and the top
  `CHANGELOG.md` entry. Pure, filesystem-free, covered by tests against synthetic strings.
- A thin script (`scripts/check_version_sync.py`) that reads the three files and runs the pure core,
  mirroring the existing `scripts/check_typing_boundaries.py` shape. It prints each mismatch and exits
  non-zero on any. Wired into CI (the `code-quality` job) and `.pre-commit-config.yaml` so version
  drift blocks a PR before merge, the same way the typing-boundary check does.
- A merge-triggered `release` job added to the existing `.github/workflows/ci.yml`. It depends on the
  existing checks, runs only on push to `main`, and is idempotent: it creates `vX.Y.Z` only when that
  tag does not already exist, after a functional smoke test of the exact commit. When the tag already
  exists it is treated as a no-op only after verifying the tag points at a commit of the matching
  version (a corrupt or stale tag fails the job); an ordinary unchanged-version merge is a no-op.
- A broadened smoke test: the pinned ref must run `check`, `lint`, and `init`, matching the
  `RELEASING.md` invariant that the tag contain all three.
- Trimming `RELEASING.md` to the human-judgment steps (bump, lock, changelog, merge) and folding the
  tag and smoke steps into the automated job, plus moving the roadmap entry from "Deferred" to
  "Shipped" and updating `CLAUDE.md` and `CHANGELOG.md`, as the closing step of the slice so the PR is
  atomic and self-consistent.

Explicitly out of scope, deferred or declined (see section 12):

- A GitHub Release object, PyPI publish, or any distribution channel other than the git tag.
  Distribution is git-tag-based; a Release object is decorative.
- Automated version bumping. Choosing the next version is a human judgment call (semver intent lives
  in the changelog the author writes); the slice verifies the bump, it does not author it.
- Changelog generation. The author writes the `## [X.Y.Z]` section; the guard only reads it.
- Non-GitHub CI providers. GitHub Actions only, matching this repo and the init scaffold.
- Any change to `check`, `impact`, `reconcile`, `graph`, `lint`, `linear`, or `init` behavior.

## 2. The failure modes this kills

`RELEASING.md` warns that "a half-done release (code merged but no tag, or a tag without the version
bump) leaves adopters with a gate that fails before `check` runs." Two distinct failures:

- **No tag.** Code merges, the human forgets `git tag` / `git push`, and every adopter's pinned
  `uvx --from git+...@vX.Y.Z` gate fails to resolve. The merge-triggered job removes the human step:
  the tag is cut by CI on the merge that bumps the version.
- **Drift.** `__version__`, `pyproject.toml`, and the changelog disagree, so the tag name, the
  installed package version, and the recorded notes diverge. The PR-time guard makes the three agree a
  merge precondition, and the release job re-asserts it before tagging.

The design principle is that the tag exists only as the output of a passing pipeline. There is no
sequence of normal actions that produces a public tag whose commit does not install and run.

## 3. Architecture and the pure/impure split

The slice respects the repo's pure-core / thin-I/O boundary. One new pure module in the package, one
thin script, and CI/pre-commit/doc wiring around them. No new package command and no new runtime
dependency.

```
pure (src, covered)        thin I/O (scripts)            CI wiring (.github, .pre-commit)
-------------------        ------------------            -------------------------------
version_check.py     <---  check_version_sync.py   <---  code-quality job step
                                                         pre-commit local hook
                                                         release job (tag + smoke)
```

`version_check.py` is the only code added under `src/doc_lattice`. Everything else is configuration
(YAML) and one script that reads files and delegates. The release job calls no package code; it
shells `uvx` against the public ref, so it tests the same path an adopter walks.

## 4. The version-consistency core (pure)

`src/doc_lattice/version_check.py` exposes one public function:

```python
def check_version_consistency(
    init_version: str, pyproject_text: str, changelog_text: str
) -> list[str]:
    """Return a message for each version source that disagrees with init_version."""
```

Behavior:

- `init_version` is the canonical version (the package `__version__`); the function compares the other
  two sources against it.
- It parses the `[project]` `version` out of `pyproject_text` with `tomllib`, and the first
  `## [X.Y.Z]` heading out of `changelog_text` with an anchored regex (the `Unreleased` placeholder, if
  present, is not a version and is skipped).
- It returns a list of mismatch messages, one per disagreeing source, each naming the file and the
  expected value, in the repo's "name the file and the fix" error style. An empty list means
  consistent.
- It does not read files, take a path, or call `datetime`. Pure and covered.

Failure-to-parse (no `version` key, no version heading in the changelog) is itself a reported
mismatch, not an exception: a changelog with no release section is a drift state the guard must flag,
not crash on.

## 5. The check_version_sync script (thin wrapper)

`scripts/check_version_sync.py` mirrors `scripts/check_typing_boundaries.py`: a module docstring, a
`main()` that reads inputs and prints, `sys.exit` on the result. It:

- imports the canonical version (`from doc_lattice import __version__`),
- reads `pyproject.toml` and `CHANGELOG.md` from the repo root (resolved relative to the script
  location, no CWD assumption),
- calls `check_version_consistency`,
- prints each returned message to stderr and exits 1 if any, else exits 0 silently.

The script holds no comparison logic; it is the I/O shell over the pure core. It is not unit-tested,
matching the precedent of `check_typing_boundaries.py` (the logic it wraps is what carries the tests),
and lives outside the coverage source (`src/doc_lattice`).

## 6. PR-time wiring

- **CI.** Add one step to the `code-quality` job in `.github/workflows/ci.yml`, after the existing
  boundary check: `uv run --no-sync python scripts/check_version_sync.py`. Drift now fails the same
  job that already enforces lint, format, types, and boundaries.
- **pre-commit.** Add a local hook to `.pre-commit-config.yaml` alongside the existing
  typing-boundary hook, so the guard runs on commit and a drifted bump never reaches a PR. The hook
  runs the same script.

Both call the identical script, so local and CI verdicts cannot diverge.

## 7. The release job

A new `release` job in `.github/workflows/ci.yml`:

- `needs: [code-quality, tests, security-scan]` so it runs only after the same-commit checks pass.
- `if: github.event_name == 'push' && github.ref == 'refs/heads/main'` so it never runs on a PR or a
  tag push, only on a landed merge to main.
- `permissions: contents: write` so it can push a tag with the default `GITHUB_TOKEN`.

Steps, in order:

1. **Derive the target tag.** Read `v{__version__}` from the checked-out package.
2. **Idempotency gate with tag-health check.** If the tag does not exist on the remote, proceed to
   step 3. If it does exist, peel it to its commit and read that commit's `__version__` with a local
   `git show vX.Y.Z:src/doc_lattice/__init__.py` (no build). Two outcomes:
   - The tagged commit's version equals `X.Y.Z`: the tag is a healthy existing release. Log the
     matched version and exit 0 as a no-op. This is the ordinary unchanged-version merge (for example
     a later docs fix), and is why a healthy tag points at its original release commit, not at HEAD.
     The job deliberately does not require the tag to point at HEAD.
   - The tagged commit's version differs from `X.Y.Z`: the tag is corrupt or stale, claiming a version
     it does not contain. Fail loudly rather than no-op, because adopters pinned to `vX.Y.Z` would
     otherwise resolve to the wrong code. This is the one preexisting state the gate must not wave
     through. (The gate cannot distinguish an intentional unchanged-version merge from a forgotten
     bump, since both look identical; the logged matched version on the no-op path makes a forgotten
     bump visible without forcing a failure on healthy docs merges.)
3. **Re-assert version sync.** Run `scripts/check_version_sync.py` again. Defense in depth; the
   `code-quality` job already ran it on this commit, but the release job must not tag on drift.
4. **Gating smoke against the commit SHA.** Run `uvx --python 3.14 --from git+<repo>@${{ github.sha }}`
   for each of `check`, `lint`, and `init`. `check` and `lint` run against a checked-in hermetic
   fixture (`tests/fixtures/release-smoke/.doc-lattice.yml`, a clean edge-free lattice) via `--config`,
   not the repository's own `docs/`, so the smoke proves the commands install and run without coupling
   release success to the state of this repo's real lattice (a future STALE or BROKEN edge under
   `docs/` must not redden every release). `init` runs in a scratch directory. Because the merge commit
   is already public on `main`, this proves the exact commit installs and all three subcommands run
   *before any tag exists*. If it fails, the job fails and no tag is created.
5. **Create and push the tag.** A lightweight `vX.Y.Z` at `github.sha`, matching the `RELEASING.md`
   convention. A lightweight tag carries no tagger identity, so it needs no `git config user.*` setup
   in the runner, and it resolves identically for `uvx ...@vX.Y.Z`. Pushed with `GITHUB_TOKEN`.
6. **Post-tag confirmation.** A cheap `uvx --from git+<repo>@vX.Y.Z doc-lattice --version` to confirm
   the pinned tag string itself resolves. The functional surface was already proven at the SHA in
   step 4; this only confirms ref resolution.

`<repo>` is `https://github.com/Guardantix/doc-lattice`, the same URL the init scaffold pins.

## 8. Data flow and exit behavior

```
PR open ──> code-quality runs check_version_sync.py ──> drift blocks the PR
                          │ green
merge to main ──> code-quality + tests + security-scan ──> release job
   │ tag vX.Y.Z already exists?
   │   yes ──> tagged commit __version__ == X.Y.Z ?
   │             yes ──> log matched version, exit 0 (healthy, no-op)
   │             no  ──> fail loudly (corrupt/stale tag)
   │   no  ──> proceed:
   ├─ assert version sync (re-check)
   ├─ smoke @SHA: check / lint / init        (gates the tag; fail => no tag)
   ├─ git tag vX.Y.Z && git push             (lightweight)
   └─ smoke @vX.Y.Z: doc-lattice --version  (confirms pinned ref resolves)
```

The only state the job creates is the tag, and only on the green path past the SHA smoke.

## 9. Error handling

- **Drift at PR or commit time.** Non-zero exit, one message per disagreeing source naming the file
  and expected value, PR (or commit) blocked.
- **SHA smoke fails.** The release job fails and no tag is created. There is nothing public to clean
  up; fix forward on a new PR.
- **Healthy tag already exists.** The tagged commit's version matches `X.Y.Z`: treated as success
  (no-op, with the matched version logged), never an error, so re-merges and re-runs are safe.
- **Corrupt or stale tag already exists.** The tagged commit's version differs from `X.Y.Z`: the job
  fails loudly rather than no-op, so a tag that resolves to the wrong code cannot pass silently. The
  job does not move or delete the existing tag (it may be load-bearing for adopters); a human resolves
  the collision deliberately.
- **Post-tag confirmation fails.** Loud job failure, but per the `RELEASING.md` rule the tag is not
  moved or deleted; recover by bumping to the next patch. This case is unlikely because the SHA smoke
  already passed; it would indicate transient ref propagation, not broken code.
- **No release loop.** A tag pushed by `GITHUB_TOKEN` does not retrigger workflows; even if a future
  config change made it, the idempotency gate makes the re-run a no-op.

## 10. Conventions and invariants

- `version_check.py` is pure and lives under `src/doc_lattice`; no `Any`/`cast` (it is not a boundary
  module), module docstring, Google-style docstring on the public function, no em-dashes, line length
  100.
- Custom errors, if any are raised, extend `ProjectError` with a `code`. The pure core prefers
  returning mismatch messages over raising; a parse failure is a reported mismatch, not an exception.
- No `datetime.now()` anywhere in the new code.
- The script reads files via paths resolved from its own location, not the CWD, so it behaves the same
  under pre-commit and CI.
- The release job pins `--python 3.14` in every `uvx` call, matching the CI matrix and the
  `RELEASING.md` smoke command.
- The release job never interpolates a `${{ }}` expression directly into a `run:` script. Step
  outputs (`steps.target.outputs.tag`/`version`) are passed in through an `env:` mapping and read as
  `"$TAG"` / `"$VERSION"`, and the commit is the built-in `$GITHUB_SHA`. So a value cannot break out
  of the shell context, making the post-merge job's injection safety structural rather than incidental.

## 11. Testing

- `tests/test_version_check.py` gives the pure core full coverage: all three agree (empty result);
  each single source disagreeing (pyproject only, changelog only); both disagreeing; a `pyproject.toml`
  with no `version` key; a `CHANGELOG.md` whose first heading is `Unreleased` (skipped) followed by a
  real version; a changelog with no version heading at all. Synthetic strings, no I/O, consistent with
  the suite, keeping coverage at or above the 80% gate.
- The script and the workflow YAML are not unit-tested, consistent with `check_typing_boundaries.py`.
  They are thin and exercised by CI itself; the first real release after this lands is the integration
  test, and the idempotency gate makes a dry merge safe to observe.

## 12. Non-goals and deferral map

| Deferred or declined item | Disposition |
|---|---|
| GitHub Release object with notes | declined; git-tag distribution makes it decorative, YAGNI |
| PyPI or other publish channel | out of scope; distribution is git-tag only |
| Automated version bumping | declined; choosing the version is human judgment, the slice verifies it |
| Changelog generation | declined; the author writes the section, the guard reads it |
| Non-GitHub CI providers | out of scope; GitHub Actions only, matching the repo and init scaffold |
| Separate `release.yml` on `workflow_run` | declined; a job in `ci.yml` gated on the existing checks is the smaller surface |

## 13. Acceptance

| Pain | Solved by | Verifiable when |
|---|---|---|
| Forgotten tag | merge-triggered release job | merging a version-bump PR cuts and pushes `vX.Y.Z` with no human tag step |
| Version drift | PR-time version-sync guard | a PR whose `__version__`, `pyproject.toml`, and changelog disagree fails `code-quality` |
| Broken pinned ref | SHA-gated check/lint/init smoke | a commit whose pinned ref cannot run all three never produces a public tag |
| Corrupt or stale tag | tag-health check on the existing-tag path | a `vX.Y.Z` that points at a non-`X.Y.Z` commit fails the release job instead of passing silently |
| Manual smoke toil | broadened automated smoke | the `RELEASING.md` checklist drops the manual tag and smoke steps, leaving only the human bump |
