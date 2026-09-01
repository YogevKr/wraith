"""Regression guards for the MCP server's threading model.

Dogfooding revealed that the browser tools, if defined as plain `def`, run
sync Playwright (Camoufox) inside FastMCP's asyncio loop and crash with
"Sync API inside the asyncio loop". They MUST be async and dispatch the
browser work to the single worker-thread executor. These offline tests lock
that contract in.
"""
import asyncio
import inspect

import wraith.mcp as m
from wraith.snapshot import Snapshot

BROWSER_TOOLS = ("navigate", "snapshot", "click", "type_text", "fill_secret", "scroll",
                 "read", "screenshot", "borrow")


def test_browser_tools_are_async():
    for name in BROWSER_TOOLS:
        fn = getattr(m, name)
        assert inspect.iscoroutinefunction(fn), (
            f"{name} must be async — a sync browser tool runs Playwright sync "
            f"inside the event loop and crashes (regression)."
        )


def test_single_worker_executor_exists():
    assert m._EXEC._max_workers == 1


def test_detect_waap_needs_no_browser_thread():
    # detect_waap is httpx-only; fine to leave sync.
    assert not inspect.iscoroutinefunction(m.detect_waap)


def test_fill_secret_parses_capability_and_returns_snapshot(monkeypatch):
    class FakeBrowser:
        def __init__(self):
            self.capability = None

        def fill_secret(self, index, capability):
            assert index == 3
            self.capability = capability
            return Snapshot("https://example.com/login", "Login", [])

    browser = FakeBrowser()
    monkeypatch.setattr(m, "_get_browser", lambda: browser)
    result = asyncio.run(
        m.fill_secret(
            3,
            {
                "provider": "instinct",
                "handle": "opaque-handle",
                "allowed_origins": ["https://example.com"],
                "field_kind": "password",
            },
        )
    )

    assert "https://example.com/login" in result
    assert browser.capability.provider == "instinct"
    assert "opaque-handle" not in result
