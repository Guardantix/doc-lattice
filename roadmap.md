# game-lattice Roadmap

Forward-looking slices, derived from the local-core design spec's deferral map
(`docs/superpowers/specs/2026-06-27-game-lattice-local-core-design.md`, section 12).
The spec is the source of truth; this file is the at-a-glance index.

## Shipped: local-core (v1)

The deterministic local engine is built and reviewed (in review on a feature branch, not yet
merged to main).
It parses lattice frontmatter and anchored sections from a tracked doc set, derives the
id-indexed edge graph on demand, and exposes the `impact`, `check`, `reconcile`, and `graph`
commands.
No network, no secrets, no LLM.

Acceptance (spec section 13), met by local-core:

| Pain | Solved by | Verifiable when |
|---|---|---|
| Discovery | `impact` over the reverse adjacency | a change to one section lists every downstream doc and ticket |
| Execution | stable ids plus `impact`-guided loading | edges survive splitting a file; `impact` points at the exact section |
| Confidence | `check` exit-code gate plus `reconcile` | a stale `seen` fails CI until consciously reconciled |

## Next spec

- `linear` command: GraphQL client and ticket-status resolution.
  This is the first network-touching slice, so it carries its own dedicated security pass
  (credentials and network are out of scope for local-core).

## Later spec

- `init` scaffolding, plus pre-commit and CI codegen.

## Deferred enhancements (no spec yet)

- Authority-ladder validation.
  `authority` is already parsed, stored, and rendered, but the ladder is not policed in local-core.
- Display-prefix lint.
  An optional future enhancement.

## Out of scope by design

- Gitignored performance cache.
  Not needed at the intended corpus size; the graph is always derived on demand, never committed.
- `split` command.
  Splitting a document is a manual or Claude-driven edit. "Execution has no command" by design;
  stable ids and `impact` make a split safe without dedicated tooling.
