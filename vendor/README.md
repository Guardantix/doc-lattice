# Vendored maintenance inputs

Repository-only assets used to regenerate and verify generated artifacts. Nothing here ships:
the sdist include list and the wheel package list in [pyproject.toml](../pyproject.toml) both
name their contents explicitly, and neither names this directory. That keeps the no-Node-runtime
boundary in [ARCHITECTURE.md](../ARCHITECTURE.md) (AD-13) intact by construction.

## github-slugger 2.0.0

`github-slugger-2.0.0.tgz` is the unmodified npm registry tarball for
[github-slugger@2.0.0](https://www.npmjs.com/package/github-slugger/v/2.0.0), the slug target
AD-13 pins. `scripts/generate_github_slugger_data.py` resolves it by default, verifies its
SHA-512 before extraction, and evaluates the extracted package under the Node version pinned in
[.nvmrc](../.nvmrc). Vendoring it makes regeneration and `--check` work offline, on a machine
that is not the one the artifact was first generated on.

| Property | Value |
| --- | --- |
| Source | `https://registry.npmjs.org/github-slugger/-/github-slugger-2.0.0.tgz` |
| Size | 6359 bytes |
| SHA-512 (hex) | `21a390f69b98b63ae4abb63462097d283667adffda89425852955ff3dcbc9326b16d11bb6354ab5ff8daba6aeff35bdceb5fa488c7a6a6e8ec337630ef0e6a73` |
| npm SRI | `sha512-IaOQ9puYtjrkq7Y0Ygl9KDZnrf/aiUJYUpVf89y8kyaxbRG7Y1SrX/jaumrv81vc61+kiMempujsM3Yw7w5qcw==` |
| npm shasum | `52cf2f9279a21eb6c59dd385b410f0c0adda8f1a` |

The hex digest is what the generator enforces, as `VENDORED_TARBALL_SHA512`. The SRI and shasum
rows are the same bytes in the two forms the registry publishes, recorded so the pin can be
cross-checked against `npm view github-slugger@2.0.0 dist` without downloading anything.

The tarball digest is the complete upstream-input identity: the Node evaluator imports
`index.js`, which in turn imports `regex.js`, so hashing the regex alone would not cover
everything executed. `UPSTREAM_REGEX_SHA256` in the generated artifact remains a narrower,
artifact-level record of the behavior that produced the stripping pattern, and the tarball's
`package/regex.js` hashes to that same value.

Upstream is ISC licensed. `github-slugger-2.0.0.LICENSE` is the license text as published, copied
out of `package/LICENSE` inside the tarball for discoverability; the tarball retains its own copy.
