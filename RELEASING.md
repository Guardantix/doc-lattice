# Releasing game-lattice

game-lattice is distributed from git, not from PyPI. The `init` command prints
pre-commit and CI snippets that pin `uvx --from git+...@vX.Y.Z`, so a release is
only complete once the matching tag exists and resolves. Cutting a release is one
atomic step: a half-done release (code merged but no tag, or a tag without the
version bump) leaves adopters with a gate that fails before `check` runs.

## Checklist

1. Bump the version to the new `X.Y.Z` in both locations:
   - `src/game_lattice/__init__.py` (`__version__`)
   - `pyproject.toml` (`version`)
2. Run `uv lock` and commit the refreshed `uv.lock`.
3. Add a `## [X.Y.Z]` section to `CHANGELOG.md`.
4. Open the PR, get it green, and merge to `main`.
5. Tag the merge commit and push the tag:

   ```bash
   git tag vX.Y.Z <merge-commit-sha>
   git push origin vX.Y.Z
   ```

6. Smoke-test the pinned ref before the release is done:

   ```bash
   uvx --python 3.14 --from git+https://github.com/Guardantix/game-lattice@vX.Y.Z game-lattice check
   ```

   It must resolve and run. If it does not, cut `X.Y.(Z+1)` rather than moving the tag.

The tag must point at a commit that contains both `check` (so the gates run) and
`init` (so adopters can run `game-lattice init` from the same ref).
