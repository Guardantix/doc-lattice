"""Boundary module: validate untyped external input into typed internal models.

This is the one place ``Any`` is permitted, per the typed-boundary policy enforced
by ``scripts/check_typing_boundaries.py``.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict


class Settings(BaseModel):
    """Example settings validated from untyped external data."""

    model_config = ConfigDict(strict=True, extra="forbid")

    name: str
    retries: int = 3


def parse_settings(data: dict[str, Any]) -> Settings:
    """Validate a raw mapping into a typed ``Settings`` model."""
    return Settings.model_validate(data)
