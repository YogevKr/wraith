"""Regression guards for the MCP server's threading model.

Dogfooding revealed that the browser tools, if defined as plain `def`, run
sync Playwright (Camoufox) inside FastMCP's asyncio loop and crash with
"Sync API inside the asyncio loop". They MUST be async and dispatch the
browser work to the single worker-thread executor. These offline tests lock
that contract in.
"""
import inspect

import wraith.mcp as m

BROWSER_TOOLS = ("navigate", "snapshot", "click", "type_text", "scroll",
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
