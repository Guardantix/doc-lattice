# game-lattice

Traceability engine for game design and production documentation

## Quick Start

### Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### Install

```bash
uv sync --group dev
```

### Run

```bash
uv run game-lattice --help
```

### Test

```bash
uv run --group dev pytest
```

### Type check

```bash
uv run --group dev ty check src
```

## Commands

| Command | What it does | Exits non-zero |
|---------|--------------|----------------|
| `check` | Classify every `derives_from` edge as OK / STALE / UNRECONCILED / BROKEN. | 1 on drift, 2 on tool error |
| `lint` | Validate the authority ladder (binding > derived > exploratory) over the edges. | 1 on a violation, 2 on tool error |
| `impact TOKEN` | List every downstream doc affected by a change to TOKEN. | 2 on tool error |
| `reconcile [ID] [--ref REF] [--all]` | Set `seen` to current upstream hashes for the selected edges (the only command that mutates your tracked docs). | 2 on tool error |
| `graph [--format mermaid\|dot]` | Emit the edge graph as Mermaid or DOT. | 2 on tool error |
| `linear [TARGET] [--from ID] [--exit-code] [--warn-exit]` | Report tickets shipped against a spec that has since drifted (needs `LINEAR_API_KEY`). | 1 with `--exit-code` on DANGER/BLOCKED, 2 on tool error |
| `init [--docs-root ...] [--linear-team KEY]` | Scaffold `.game-lattice.yml` and print pre-commit and CI codegen. | 2 on tool error |

Every command except `init` accepts `--config PATH` (path to `.game-lattice.yml`; defaults to the file in the current directory). `check`, `lint`, `impact`, and `linear` accept `--json` for machine-readable output. Run `uv run game-lattice <command> --help` for the full flag list.

## Environment

`game-lattice linear` fetches live ticket status over the Linear GraphQL API and reads `LINEAR_API_KEY` from the environment; export it before running `linear` (the error message points you to `impact` for the offline view). Every other command runs fully offline. Set the team the query targets with `linear_team` in `.game-lattice.yml` (or pass `--linear-team` to `init` to bake it in).

## Adopting game-lattice in your docs repo

Bootstrap config and the drift and authority-ladder gates for a repo whose docs you want to track:

```bash
uvx --python 3.14 --from git+https://github.com/Guardantix/game-lattice@v0.3.0 game-lattice init
```

This writes `.game-lattice.yml` (only if absent) and prints pre-commit hooks and
a GitHub Actions workflow that run `game-lattice check` (drift) and `game-lattice lint`
(authority ladder) as your gates. Paste each where the output says. Pass `--docs-root`
(repeatable) or `--linear-team` to bake those values into the generated config.

## Documentation

| Document | Purpose |
|----------|---------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design and decisions |
| [build-log.md](build-log.md) | Development timeline |
| [roadmap.md](roadmap.md) | Planned capabilities |
| [CLAUDE.md](CLAUDE.md) | AI assistant instructions |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [RELEASING.md](RELEASING.md) | Release checklist and version-tag procedure |

## Project Structure

```
game-lattice/
├── src/game_lattice/    # Source code
├── tests/                    # Test suite
├── procedures/               # Coding conventions
└── pyproject.toml            # Project configuration
```
