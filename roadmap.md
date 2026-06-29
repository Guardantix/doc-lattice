# game-lattice Roadmap

Forward-looking slices, derived from the local-core design spec's deferral map
(`docs/superpowers/specs/2026-06-27-game-lattice-local-core-design.md`, section 12).
The spec is the source of truth; this file is the at-a-glance index.

## Shipped

- **local-core (v1)** (PR #1). The deterministic local engine: lattice parse, the id-indexed edge
  graph derived on demand, and the `impact`, `check`, `reconcile`, and `graph` commands. No network,
  no secrets, no LLM. Spec: `docs/superpowers/specs/2026-06-27-game-lattice-local-core-design.md`.
- **linear slice** (PR #3). The `linear` command resolves referenced tickets to live status and
  reports shipped-against-stale-spec drift. The first network-touching slice. Spec:
  `docs/superpowers/specs/2026-06-27-game-lattice-linear-design.md`.
- **init slice** (PR #4). The `init` command scaffolds `.game-lattice.yml` and prints pre-commit
  and CI codegen for an adopting repo. Shipped as the 0.2.0 release (tag `v0.2.0`). Spec:
  `docs/superpowers/specs/2026-06-28-game-lattice-init-design.md`.
- **lint slice** (v0.3.0). The `lint` command validates the authority ladder over `derives_from`
  edges, reports edges it cannot rank, and is wired into the generated pre-commit and CI gates
  alongside `check`. Spec: `docs/superpowers/specs/2026-06-28-game-lattice-lint-design.md`.

Acceptance (local-core spec section 13), still met:

| Pain | Solved by | Verifiable when |
|---|---|---|
| Discovery | `impact` over the reverse adjacency | a change to one section lists every downstream doc and ticket |
| Execution | stable ids plus `impact`-guided loading | edges survive splitting a file; `impact` points at the exact section |
| Confidence | `check` exit-code gate plus `reconcile` | a stale `seen` fails CI until consciously reconciled |

## Deferred enhancements (no spec yet)

- Release-tag automation. CI that creates and verifies the `vX.Y.Z` tag, replacing the manual
  `RELEASING.md` checklist with a machine-checked smoke test. Recorded by the init spec (section 11).
- Display-prefix lint. An optional future enhancement.

## Out of scope by design

- Gitignored performance cache. Not needed at the intended corpus size; the graph is always derived
  on demand, never committed.
- `split` command. Splitting a document is a manual or Claude-driven edit. "Execution has no command"
  by design; stable ids and `impact` make a split safe without dedicated tooling.
