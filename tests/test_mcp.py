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
                 "read", "screenshot", "borrow", "receive_profile")


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


def test_receive_profile_pulls_and_injects(monkeypatch):
    import wraith.profile as profile_mod

    jar = {"cookies": [{"name": "sid", "value": "abc", "domain": "elal.com"}], "origins": []}
    monkeypatch.setattr(profile_mod, "receive_profile", lambda code: jar)

    injected = {}

    class FakeCtx:
        def add_cookies(self, payload):
            injected["payload"] = payload

    monkeypatch.setattr(m, "_get_browser", lambda: object())
    monkeypatch.setattr(m, "_ctx_from_browser", lambda b: FakeCtx())

    result = asyncio.run(m.receive_profile("wraith1.aaa.bbb"))
    assert injected["payload"][0]["name"] == "sid"
    assert "elal.com" in result
    # The pairing code / cookie value must not leak into the tool result.
    assert "abc" not in result


def test_receive_profile_reports_pull_failure(monkeypatch):
    import wraith.profile as profile_mod

    def boom(code):
        raise RuntimeError("no drop at this slot")

    monkeypatch.setattr(profile_mod, "receive_profile", boom)
    result = asyncio.run(m.receive_profile("wraith1.aaa.bbb"))
    assert "could not receive" in result
    assert "no drop at this slot" in result
