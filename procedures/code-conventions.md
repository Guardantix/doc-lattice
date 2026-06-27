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

- Custom exceptions must extend `ProjectError`
- No bare `except Exception` or `except BaseException`
- Error messages must be actionable

## Dependencies

- Pin minimum versions in `pyproject.toml`
- Dev dependencies go in the `dev` group under `[dependency-groups]` (PEP 735)

## Security

- No `datetime.now()` outside `datetime_utils.py`
- No `innerHTML` in any file
- No hardcoded secrets
- All paths must use `safe_resolve()` for user-provided paths

## Constants

- Use `Literal` + `get_args()` + `frozenset` pattern
- Define in `constants.py`, import elsewhere
- No raw string literals that duplicate constant values
