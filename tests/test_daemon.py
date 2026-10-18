"""The background session behind `wraith call` (no browser needed)."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from wraith import daemon


@pytest.fixture
def session_dir(monkeypatch):
    # A short folder: a Unix socket path must stay under about 100 bytes, and
    # pytest's tmp_path is too long on macOS.
    folder = tempfile.mkdtemp(prefix="wr-", dir="/tmp")
    os.chmod(folder, 0o700)
    monkeypatch.setenv(daemon.ENV_SESSION_DIR, folder)
    yield Path(folder)
    shutil.rmtree(folder, ignore_errors=True)


async def navigate(url: str, include_snapshot: bool = True) -> str:
    """Open a URL."""
    return f"{url} {include_snapshot}"


async def drag(dx: float, dy: float = 0.0, index: Optional[int] = None) -> str:
    return f"{dx} {dy} {index}"


async def upload_file(index: int, paths: list[str]) -> str:
    return f"{index} {paths}"


async def fill_secret(index: int, capability: dict) -> str:
    return f"{index} {capability}"


@pytest.mark.parametrize(
    "fn, argv, expected",
    [
        (
            navigate,
            ["https://x.test/?a=b"],
            {"url": "https://x.test/?a=b", "include_snapshot": True},
        ),
        (
            navigate,
            ["u", "--no-include-snapshot"],
            {"url": "u", "include_snapshot": False},
        ),
        (drag, ["-40", "--index", "3"], {"dx": -40.0, "dy": 0.0, "index": 3}),
        (
            upload_file,
            ["2", "a.txt", "b.txt"],
            {"index": 2, "paths": ["a.txt", "b.txt"]},
        ),
        (
            fill_secret,
            ["1", '{"provider": "env"}'],
            {"index": 1, "capability": {"provider": "env"}},
        ),
    ],
)
def test_tool_parser_follows_the_signature(fn, argv, expected):
    assert vars(daemon.tool_parser(fn.__name__, fn).parse_args(argv)) == expected


@pytest.mark.parametrize(
    "argv, status, text",
    [
        (["--help"], 0, "Open a URL."),
        ([], 2, "the following arguments are required: url"),
    ],
)
def test_tool_parser_returns_help_and_errors(argv, status, text):
    with pytest.raises(daemon._ParseExit) as caught:
        daemon.tool_parser("navigate", navigate).parse_args(argv)
    assert caught.value.status == status
    assert text in caught.value.text


def test_file_arguments_resolve_against_the_callers_folder():
    kwargs = {
        "paths": ["a.txt", "/abs/b.txt"],
        "path": "state.json",
        "session_file": "",
        "url": "x",
    }
    daemon._resolve_paths(kwargs, "/work")
    assert kwargs == {
        "paths": ["/work/a.txt", "/abs/b.txt"],
        "path": "/work/state.json",
        "session_file": "",
        "url": "x",
    }


def test_bad_names_and_open_folders_are_refused(session_dir):
    with pytest.raises(daemon.SessionError, match="bad session name"):
        daemon.socket_path("../x")
    os.chmod(session_dir, 0o755)
    with pytest.raises(daemon.SessionError, match="open to other users"):
        daemon.socket_path("ok")


class _Shot:
    data = b"\x89PNG fake"


def _fake_tools():
    calls = {"n": 0}

    async def count() -> str:
        """Count calls."""
        calls["n"] += 1
        return str(calls["n"])

    async def fail() -> str:
        raise RuntimeError("boom")

    async def screenshot() -> object:
        return _Shot()

    def lookup(url: str) -> list[str]:
        return ["Cloudflare", url]

    async def confirm(index: int) -> bool:
        return index == 1

    return {f.__name__: f for f in (count, fail, screenshot, lookup, confirm)}


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = 0

    def is_open(self) -> bool:
        return False

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture
def server(session_dir):
    """A session server with fake tools, run on a thread."""
    browser = _FakeBrowser()
    srv = daemon._Server("t", _fake_tools(), 0.0, browser)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=loop.run_until_complete,
        args=(srv.run(daemon.socket_path("t")),),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while daemon.ping("t") is None:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    yield browser
    if daemon.ping("t") is not None:
        daemon.stop("t", wait=5)
    thread.join(5)
    loop.close()


def test_calls_share_one_session_and_report_failures(server, tmp_path):
    assert daemon.call("t", "count", [])["output"] == "1"
    assert daemon.call("t", "count", [])["output"] == "2"
    assert (
        daemon.call("t", "lookup", ["https://x.test"])["output"]
        == "Cloudflare\nhttps://x.test"
    )
    assert daemon.call("t", "fail", []) == {
        "ok": False,
        "output": "RuntimeError: boom",
        "status": 1,
    }
    assert daemon.call("t", "confirm", ["2"]) == {
        "ok": False,
        "output": "false",
        "status": 1,
    }
    unknown = daemon.call("t", "nope", [])
    assert unknown["status"] == 2 and "count" in unknown["output"]
    saved = daemon.call("t", "screenshot", ["--out", "shot.png"], cwd=str(tmp_path))
    assert saved["ok"] and (tmp_path / "shot.png").read_bytes() == _Shot.data
    assert "count" in daemon.tools("t")


def test_stop_closes_the_browser_and_removes_the_socket(server):
    assert daemon.stop("t", wait=5) is True
    assert server.closed == 1
    assert not daemon.socket_path("t").exists()
    assert daemon.stop("t") is False


def test_an_idle_session_stops_by_itself(session_dir):
    srv = daemon._Server("idle", {}, 0.2, _FakeBrowser())
    started = time.monotonic()
    asyncio.run(srv.run(daemon.socket_path("idle")))
    assert time.monotonic() - started < 5
    assert not daemon.socket_path("idle").exists()


def test_start_runs_the_real_server_without_leaking_launch_options(session_dir):
    secret = "pw-" + os.urandom(4).hex()
    info = daemon.start(
        "real",
        idle_timeout=60,
        launch={"proxy": f"http://u:{secret}@127.0.0.1:9", "engine": "auto"},
    )
    try:
        command = subprocess.run(
            ["ps", "-o", "command=", "-p", str(info["pid"])],
            capture_output=True,
            text=True,
        ).stdout
        assert "wraith.daemon" in command and secret not in command
        assert info["browser_open"] is False
        with pytest.raises(daemon.SessionError, match="already running"):
            daemon.start("real")
        again, started = daemon.ensure("real")
        assert started is False and again["pid"] == info["pid"]
        helped = daemon.call("real", "navigate", ["--help"])
        assert helped["ok"] and "usage: wraith call navigate" in helped["output"]
        assert "navigate" in daemon.tools("real")
    finally:
        assert daemon.stop("real") is True
    assert daemon.ping("real") is None


@dataclasses.dataclass
class _FakeSessions:
    """``ping`` / ``_spawn`` stand-ins: a session answers once its spawn ends."""

    stopping_pings: int = 0
    running: dict = dataclasses.field(default_factory=dict)
    spawned: list = dataclasses.field(default_factory=list)

    def ping(self, name):
        if self.stopping_pings:
            self.stopping_pings -= 1
            return {"pid": 0, "stopping": True}
        return self.running.get(name)

    def spawn(self, name, config, wait):
        self.spawned.append(name)
        time.sleep(0.2)
        self.running[name] = {"pid": 1}
        return self.running[name]


def test_two_callers_get_one_session(session_dir, monkeypatch):
    fake = _FakeSessions()
    monkeypatch.setattr(daemon, "ping", fake.ping)
    monkeypatch.setattr(daemon, "_spawn", fake.spawn)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(daemon.ensure("t")))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert fake.spawned == ["t"]
    assert sorted(started for _, started in results) == [False, True]


def test_a_call_waits_for_a_stopping_session_then_starts_a_new_one(
    session_dir, monkeypatch
):
    fake = _FakeSessions(stopping_pings=3)
    monkeypatch.setattr(daemon, "ping", fake.ping)
    monkeypatch.setattr(daemon, "_spawn", fake.spawn)
    assert daemon.ensure("t") == ({"pid": 1}, True)
    assert fake.spawned == ["t"]
