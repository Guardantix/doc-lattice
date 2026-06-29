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
uv run ty check src/
```

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

## Project Structure

```
game-lattice/
├── src/game_lattice/    # Source code
├── tests/                    # Test suite
├── procedures/               # Coding conventions
└── pyproject.toml            # Project configuration
```
