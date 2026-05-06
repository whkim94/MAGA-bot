from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_truthy(name: str, *, default: bool = False) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    return value in _TRUTHY


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    if minimum is not None:
        return max(minimum, value)
    return value
