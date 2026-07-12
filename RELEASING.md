# Releasing doc-lattice

doc-lattice publishes immutable `vX.Y.Z` tags, GitHub Releases, wheels, and source
distributions. PyPI Trusted Publishing trusts the `Guardantix/doc-lattice` repository, the
`ci.yml` workflow, and the `pypi` environment; no PyPI API token is stored.

## Release checklist

1. Bump the version in `src/doc_lattice/__init__.py`, `pyproject.toml`, the newest
   `CHANGELOG.md` heading, and every released-version pin in `README.md`.
2. Run `uv lock` and commit the refreshed `uv.lock`.
3. Confirm the new changelog section is nonempty.
4. Run the full verification suite, open a pull request, and wait for every CI check to pass.
5. Merge the pull request to `main`.

The release pipeline then runs in this order:

1. The `release` job smoke-tests the release source, creates the immutable `vX.Y.Z` tag, and
   creates its GitHub Release.
2. The dependent, unprivileged `build-release` job checks out that exact tag, builds the wheel
   and source distribution, validates both with Twine, and uploads them as an artifact.
3. The `publish` job downloads the validated artifact and uploads it to PyPI through OIDC. It
   does not check out the repository or execute package build code.

After publication, confirm the public index serves the release:

```bash
uvx --refresh doc-lattice --version
```

The command must print the released version.

## Release semantics and recovery

- An ordinary merge that leaves the version unchanged is a no-op.
- Rerunning the workflow for the commit already referenced by the matching tag resumes any
  missing GitHub Release or PyPI publication steps. Publication uses `skip-existing`, so a
  retry neither re-uploads existing PyPI files nor fails because they already exist.
- A commit with an unchanged version whose tag points to an older commit is a no-op.
- A matching tag that points to a source with a different version fails the release.
- When the tag is absent, only the commit that introduces that version may create it. A missing
  version file in its first parent can identify the package and version introduction.
- Malformed current, parent, or tagged version declarations fail closed. Unexpected Git or
  source-reading failures also fail closed; they are never treated as permission to publish.

If any release step fails, rerun the same workflow. Never move a release tag or delete or
replace files already published to PyPI. If the release source itself is wrong, fix it and cut
the next version.

## Local verification

Run the same source checks used by CI:

```bash
env -u FORCE_COLOR uv run --locked --group dev pytest
uv run --locked --group dev ruff check src tests scripts/release_gate.py
uv run --locked --group dev ruff format --check src tests scripts/release_gate.py
uv run --locked --group dev ty check src scripts/release_gate.py
uv run --locked --group dev python scripts/check_typing_boundaries.py src
uv run --locked --group dev python scripts/check_version_sync.py
uv build
uvx --from twine twine check dist/*
```

Smoke-test the built wheel in a fresh Python 3.13 environment:

```bash
tmpdir="$(mktemp -d)"
uv venv --python 3.13 "$tmpdir/.venv"
uv pip install --python "$tmpdir/.venv/bin/python" dist/*.whl
"$tmpdir/.venv/bin/doc-lattice" --version
```
