"""Shared stand-ins for Playwright objects in offline tests."""

from __future__ import annotations

from typing import Any


class NodeHandle:
    """What ``evaluate_handle`` returns for the snapshot's index lookup."""

    def __init__(self, node: Any) -> None:
        self._node = node

    def as_element(self) -> Any:
        return self._node


def mcp_browser_kwargs(monkeypatch: Any) -> dict:
    """Start the MCP server's browser with a stand-in; return its kwargs."""
    import wraith.agent as ag
    import wraith.mcp as m

    seen: dict = {}

    class FakeAgentBrowser:
        def __init__(self, **kw: Any) -> None:
            seen.update(kw)

    monkeypatch.setattr(ag, "AgentBrowser", FakeAgentBrowser)
    monkeypatch.setattr(m, "_browser", None)
    monkeypatch.setattr(m, "_LAUNCH_KW", {})
    m._get_browser()
    return seen


def cli_mcp_calls(monkeypatch: Any, argv: list[str]) -> list[tuple]:
    """Run ``wraith mcp ARGV`` against a stand-in that records each call."""
    from wraith import cli

    calls: list[tuple] = []

    class Recorder:
        def __getattr__(self, name: str) -> Any:
            return lambda *args: calls.append((name, *args))

    monkeypatch.setattr(cli, "_lazy", lambda name: Recorder())
    cli.main(["mcp", *argv])
    return calls
