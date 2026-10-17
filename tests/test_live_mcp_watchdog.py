"""Opt-in: WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_mcp_watchdog.py.

A page script that never returns blocks Playwright's evaluate. The MCP server
must report it, kill that browser, and serve the next call with a new one.
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip(
        "set WRAITH_LIVE_TEST=1 to run the watchdog check", allow_module_level=True
    )

import wraith.mcp as m
from tests._local_server import html_pages, serve

PAGE = (
    "<!doctype html><title>Loop</title><button>Still here</button>" + "<p>text</p>" * 40
)


def test_a_stuck_page_does_not_freeze_the_server(monkeypatch):
    monkeypatch.setenv(m.ENV_TOOL_TIMEOUT, "120")  # a launch and a page load fit
    monkeypatch.setattr(m, "_EXEC", ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(m, "_browser", None)
    monkeypatch.setattr(m, "_LAUNCH_KW", {"geoip": False})
    with serve(html_pages({}, PAGE)) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        try:
            asyncio.run(m._run(lambda: m._get_browser().navigate(url)))
            driver = m._driver_pid(m._browser)
            assert driver is not None
            monkeypatch.setenv(m.ENV_TOOL_TIMEOUT, "10")
            with pytest.raises(m._ToolError, match="BrowserStuck"):
                asyncio.run(
                    m._run(
                        lambda: m._get_browser().page.evaluate(
                            "() => { while (true) {} }"
                        )
                    )
                )
            with pytest.raises(OSError):
                os.kill(driver, 0)  # the stuck browser's driver is gone
            monkeypatch.setenv(m.ENV_TOOL_TIMEOUT, "120")
            snap = asyncio.run(m._run(lambda: m._get_browser().navigate(url)))
            assert any(el.text == "Still here" for el in snap.elements)
            assert m._driver_pid(m._browser) != driver  # a new browser
        finally:
            m._EXEC.submit(m._reset_browser).result(timeout=60)
