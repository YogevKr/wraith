"""Opt-in: WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_cli_session.py.

Separate `wraith call` commands must drive one browser through a background
session, and `wraith session stop` must leave no browser process behind.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip(
        "set WRAITH_LIVE_TEST=1 to run the CLI session check", allow_module_level=True
    )

from tests._local_server import html_pages, serve
from tests.test_live_mcp_watchdog import _exited

PAGE = (
    "<!doctype html><title>Counter</title>"
    '<button onclick="n.textContent = +n.textContent + 1">Add one</button>'
    "<p>Count: <span id=n>0</span></p>" + "<p>text</p>" * 40
)


def _wraith(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "wraith.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )


def _descendants(pid: int) -> list[int]:
    table = subprocess.run(
        ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True
    ).stdout
    children: dict[int, list[int]] = {}
    for line in table.splitlines():
        child, parent = (int(part) for part in line.split())
        children.setdefault(parent, []).append(child)
    found = [pid]
    for parent in found:
        found.extend(children.get(parent, []))
    return found[1:]


@pytest.mark.parametrize("engine", ["camoufox", "chromium"])
def test_cli_calls_share_one_browser(engine):
    folder = tempfile.mkdtemp(prefix="wr-", dir="/tmp")
    os.chmod(folder, 0o700)
    env = {**os.environ, "WRAITH_SESSION_DIR": folder}
    name = f"live-{engine}"
    pid, browser = 0, []
    try:
        with serve(html_pages({}, PAGE)) as server:
            url = f"http://127.0.0.1:{server.server_port}/"
            started = _wraith(
                env,
                "session",
                "start",
                "--name",
                name,
                "--engine",
                engine,
                "--no-geoip",
                "--idle-timeout",
                "300",
            )
            assert started.returncode == 0, started.stderr
            pid = int(re.search(r"pid (\d+)", started.stdout).group(1))

            opened = _wraith(env, "call", "--session", name, "navigate", url)
            assert opened.returncode == 0, opened.stderr
            button = re.search(r"\[(\d+)\][^\n]*Add one", opened.stdout).group(1)
            for _ in range(2):
                clicked = _wraith(
                    env,
                    "call",
                    "--session",
                    name,
                    "click",
                    button,
                    "--no-include-snapshot",
                )
                assert clicked.returncode == 0, clicked.stderr

            page = _wraith(env, "call", "--session", name, "read")
            assert "Count: 2" in page.stdout, page.stdout
            status = _wraith(env, "session", "status")
            assert f"{name}: pid {pid}, browser open" in status.stdout
            browser = _descendants(pid)
            assert browser, "the session has no browser process"
    finally:
        stopped = _wraith(env, "session", "stop", "--name", name)
        shutil.rmtree(folder, ignore_errors=True)
    assert f"session '{name}' stopped" in stopped.stdout
    assert _exited(pid)
    assert all(_exited(child) for child in browser)
