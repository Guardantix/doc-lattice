# Releasing doc-lattice

doc-lattice publishes immutable `vX.Y.Z` tags, GitHub Releases, wheels, and source
distributions. PyPI Trusted Publishing trusts the `Guardantix/doc-lattice` repository, the
`ci.yml` workflow, and the `pypi` environment; no PyPI API token is stored.

## Release checklist

1. Bump the version in `src/doc_lattice/__init__.py`, `pyproject.toml`, and every
   released-version pin in `README.md` and `MANAGED_CI.md`, and promote `## [Unreleased]` in `CHANGELOG.md` to
   `## [X.Y.Z] - YYYY-MM-DD` so it becomes the first versioned heading, which is the heading
   the version-sync guard reads.
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

If any release step fails, rerun the same workflow. Never move a release tag or delete or
replace files already published to PyPI. If the release source itself is wrong, fix it and cut
the next version.

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
