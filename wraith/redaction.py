"""Redact sensitive values at browser and process boundaries.

Wraith never includes secret material in interaction errors or managed-run
output. Callers pass values only for the duration of one operation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = ["redact_text", "redacted_exception"]


def redact_text(value: Any, secrets: Iterable[Any] = ()) -> str:
    """Return text with every non-empty sensitive value replaced."""
    text = str(value)
    replacements = sorted(
        {str(item) for item in secrets if item not in (None, "")},
        key=len,
        reverse=True,
    )
    for secret in replacements:
        text = text.replace(secret, "[REDACTED]")
    return text


def redacted_exception(error: BaseException, secrets: Iterable[Any] = ()) -> RuntimeError:
    """Discard driver messages, which can contain escaped or partial values."""
    return RuntimeError(f"{type(error).__name__}: [REDACTED] browser operation failed")
