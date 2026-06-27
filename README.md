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
