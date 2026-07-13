# doc-lattice Roadmap

Forward-looking slices plus shipped behavior. Historical specs and plans under
`docs/superpowers/` explain how slices were designed and implemented, but current code and
supported documentation supersede them when behavior has changed.

## Shipped

- **local-core (v1)** (PR #1). The deterministic local engine: lattice parse, the id-indexed edge
  graph derived on demand, and the `impact`, `check`, `reconcile`, and `graph` commands. No network,
  no secrets, no LLM. Historical spec:
  `docs/superpowers/specs/2026-06-27-doc-lattice-local-core-design.md`.
- **linear slice** (PR #3). The `linear` command resolves referenced tickets to live status and
  reports shipped-against-stale-spec drift. The first network-touching slice. Historical spec:
  `docs/superpowers/specs/2026-06-27-doc-lattice-linear-design.md`.
- **init slice** (PR #4). The `init` command scaffolds `.doc-lattice.yml` and prints pre-commit
  and CI codegen for an adopting repo. Shipped as the 0.2.0 release (tag `v0.2.0`). Historical spec:
  `docs/superpowers/specs/2026-06-28-doc-lattice-init-design.md`.
- **lint slice** (v0.3.0). The `lint` command validates the authority ladder over `derives_from`
  edges, reports edges it cannot rank, and is wired into the generated pre-commit and CI gates
  alongside `check`. Historical spec:
  `docs/superpowers/specs/2026-06-28-doc-lattice-lint-design.md`.
- **release automation and PyPI publishing** (v1.0.0). The version-sync guard covers
  `__version__`, `pyproject.toml`, the top versioned changelog entry, and exact README pins. A
  merge-triggered release job validates or creates the `vX.Y.Z` tag; an unprivileged job builds
  and validates distributions from that exact tag; an OIDC-only job publishes the transferred
  artifact to PyPI without checking out repository code. Historical specs:
  `docs/superpowers/specs/2026-06-29-doc-lattice-release-automation-design.md` and
  `docs/superpowers/specs/2026-07-12-pypi-publishing-design.md`.
- **incremental load cache (v0.8.0)**. An opt-in `cache_key` shares parsed document derivations
  across runs and worktrees. The default verify tier re-reads and hashes document bytes, while
  `cache_trust_stat: true` enables the faster size/mtime tier for read-only commands under its
  documented caveat. Reconcile always uses verified loads before planning writes. Historical spec:
  `docs/superpowers/specs/2026-07-10-doc-lattice-load-cache-design.md`.

Acceptance (local-core spec section 13), still met:

| Pain | Solved by | Verifiable when |
|---|---|---|
| Discovery | `impact` over the reverse adjacency | a change to one section lists every downstream doc and ticket |
| Execution | stable ids plus `impact`-guided loading | edges survive splitting a file; `impact` points at the exact section |
| Confidence | `check` exit-code gate plus `reconcile` | a stale `seen` fails CI until consciously reconciled |

## Deferred enhancements (no spec yet)

- Display-prefix lint. An optional future enhancement.

## Out of scope by design

- `split` command. Splitting a document is a manual or Claude-driven edit. "Execution has no command"
  by design; stable ids and `impact` make a split safe without dedicated tooling.
