"""Shared stand-ins for Playwright objects in offline tests."""

from __future__ import annotations

from typing import Any


class NodeHandle:
    """What ``evaluate_handle`` returns for the snapshot's index lookup."""

    def __init__(self, node: Any) -> None:
        self._node = node

    def as_element(self) -> Any:
        return self._node
