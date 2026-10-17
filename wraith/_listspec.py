"""Shared parsing for policies built from a list or a separated string."""

from __future__ import annotations

from typing import Any, Optional, TypeVar

P = TypeVar("P", bound="ListSpec")


class ListSpec:
    """A policy made from items: a list, or one string split on ``SEPARATOR``."""

    SEPARATOR = ","

    def __init__(self, items: Any) -> None:  # pragma: no cover - subclasses define it
        raise NotImplementedError

    @classmethod
    def parse(cls: type[P], value: Any) -> Optional[P]:
        """Build the policy from ``value``; ``None`` when it names no item."""
        if value is None or isinstance(value, cls):
            return value
        items = value.split(cls.SEPARATOR) if isinstance(value, str) else list(value)
        if not any(str(item).strip() for item in items):
            return None
        return cls(items)
