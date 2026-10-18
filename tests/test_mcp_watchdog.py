"""A browser call that never returns must not freeze the MCP server."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import wraith.mcp as m


def _browser_with_driver(pid: int):
    transport = SimpleNamespace(_proc=SimpleNamespace(pid=pid))
    page = SimpleNamespace(
        _impl_obj=SimpleNamespace(_connection=SimpleNamespace(_transport=transport))
    )
    return SimpleNamespace(_session=SimpleNamespace(page=page))


def test_a_stuck_call_is_reported_and_its_driver_killed(monkeypatch):
    driver = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    release = threading.Event()
    monkeypatch.setenv(m.ENV_TOOL_TIMEOUT, "1")
    monkeypatch.setattr(m, "_EXEC", ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(m, "_browser", _browser_with_driver(driver.pid))
    old_executor = m._EXEC
    try:
        with pytest.raises(
            m._ToolError, match="BrowserStuck: the browser did not answer within 1 s"
        ):
            asyncio.run(m._run(lambda: release.wait(30)))
        assert driver.wait(timeout=5) != 0  # killed
        assert m._browser is None and m._EXEC is not old_executor
        assert asyncio.run(m._run(lambda: "fresh worker")) == "fresh worker"
    finally:
        release.set()
        if driver.poll() is None:
            driver.kill()


def test_a_tool_timeout_is_not_reported_as_a_stuck_browser(monkeypatch):
    # wait_for raises the built-in TimeoutError when its text does not show.
    # Since Python 3.11 that is also asyncio.TimeoutError, which the watchdog
    # once took for a stuck browser: it closed the browser and said 180 s.
    browser = object()
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(m, "_EXEC", executor)
    monkeypatch.setattr(m, "_browser", browser)

    def text_never_shows():
        raise TimeoutError("'Create New Policy' did not appear within 60 s")

    with pytest.raises(m._ToolError) as err:
        asyncio.run(m._run(text_never_shows))
    assert str(err.value) == (
        "TimeoutError: 'Create New Policy' did not appear within 60 s"
    )
    assert m._browser is browser and m._EXEC is executor  # browser kept


def test_a_cancelled_call_that_waits_in_the_queue_never_runs(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(m, "_EXEC", executor)
    release = threading.Event()
    ran = []

    async def scenario():
        busy = asyncio.ensure_future(m._run(lambda: release.wait(10)))
        queued = asyncio.ensure_future(m._run(lambda: ran.append("queued")))
        await asyncio.sleep(0.1)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await busy

    asyncio.run(scenario())
    executor.shutdown(wait=True)
    assert ran == []


def test_tool_timeout_setting(monkeypatch):
    monkeypatch.delenv(m.ENV_TOOL_TIMEOUT, raising=False)
    assert m._tool_timeout() == 180
    monkeypatch.setenv(m.ENV_TOOL_TIMEOUT, "0.1")
    assert m._tool_timeout() == 1
    monkeypatch.setenv(m.ENV_TOOL_TIMEOUT, "soon")
    assert m._tool_timeout() == 180
    assert m._driver_pid(object()) is None


def test_kill_tree_kills_children(tmp_path):
    marker = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time; "
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(marker)!r}, 'w').write(str(c.pid)); time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", code])
    deadline = time.time() + 10
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    child = int(marker.read_text())
    m._kill_tree(parent.pid)
    parent.wait(timeout=5)
    for _ in range(50):
        try:
            os.kill(child, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the child process survived")
