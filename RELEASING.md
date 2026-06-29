# Releasing game-lattice

game-lattice is distributed from git, not from PyPI. The `init` command prints
pre-commit and CI snippets that pin `uvx --from git+...@vX.Y.Z`, so a release is
only complete once the matching tag exists and resolves. The version bump is the
human step; CI cuts and verifies the tag on merge, so a half-done release (code
merged but no tag, or a tag without the version bump) cannot land.

## Checklist

1. Bump the version to the new `X.Y.Z` in both locations:
   - `src/game_lattice/__init__.py` (`__version__`)
   - `pyproject.toml` (`version`)
2. Run `uv lock` and commit the refreshed `uv.lock`.
3. Add a `## [X.Y.Z]` section to `CHANGELOG.md` (rename the `## [Unreleased]`
   section if you have been accumulating notes there).
4. Open the PR and get it green. The `check-version-sync` gate fails the PR if the
   three version sources disagree, so fix any drift before merge.
5. Merge to `main`. On that push, the `release` job:
   - verifies version sync again,
   - smoke-tests the exact commit over `git+...@<sha>`, running `check`, `lint`,
     and `init`,
   - creates and pushes the lightweight `vX.Y.Z` tag,
   - confirms the pinned `@vX.Y.Z` ref resolves.

   An ordinary merge that does not change the version is a safe no-op: the job
   confirms the existing tag points at a commit of the matching version.

If the release job fails after the tag is pushed, do not move the tag: cut
`X.Y.(Z+1)` instead, because a moved tag breaks adopters already pinned to the
old one.

The tag must point at a commit that contains `check`, `lint`, and `init`, so the
gates run and adopters can run `game-lattice init` from the same ref. The release
job's smoke step enforces exactly this before the tag is created.
