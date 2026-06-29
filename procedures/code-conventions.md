# Code Conventions (Python)

## File Documentation

Every Python module must have a module-level docstring describing its purpose.

## Function Documentation

Use Google-style docstrings for all public functions:

```python
def function_name(param: str) -> int:
    """Brief description.

    Args:
        param: Description of param.

    Returns:
        Description of return value.

    Raises:
        ValueError: When param is invalid.
    """
```

## Testing

- All tests use pytest
- Test files mirror source files: `src/pkg/foo.py` -> `tests/test_foo.py`
- Use `tmp_path` for filesystem tests
- No mocking of internal modules unless necessary

## Error Handling

- Custom exceptions must extend `ProjectError` and carry a `code` (see `error_types.py`)
- No bare `except Exception` or `except BaseException`
- Error messages must name the file and the fix

## Dependencies

- Pin minimum versions in `pyproject.toml`
- Dev dependencies go in the `dev` group under `[dependency-groups]` (PEP 735)

## Security

- No `datetime.now()` or `datetime.utcnow()` outside `datetime_utils.py`
- No `innerHTML` in any file
- No hardcoded secrets
- All paths must use `safe_resolve()` for user-provided paths

## Constants

- Use `Literal` + `get_args()` + `frozenset` pattern
- Define in `constants.py`, import elsewhere
- No raw string literals that duplicate constant values

## Typing Boundary

- `typing.Any` and `typing.cast` are allowed only in boundary modules: a file whose stem is one of `boundary`, `adapter`, `parser`, `validator`, `external`, or `inbound`, or ends with one of those words prefixed by `_` (for example `frontmatter_parser`), or that sits under a directory of one of those names.
- Everywhere else, convert untyped external data at the boundary and pass typed models inward.
- Enforced by `scripts/check_typing_boundaries.py` in pre-commit and CI. The real engine boundaries are `frontmatter_parser.py` (raw YAML to `NodeMeta`) and `linear_parser.py` (Linear JSON to `Ticket`).

## Formatting

- Line length is 100 (`[tool.ruff]` in `pyproject.toml`); ruff and ruff-format enforce it in pre-commit and CI.
- No em-dashes in any drafted content (docstrings, messages, comments); use `--` or rephrase.
