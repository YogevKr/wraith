"""Offline tests for press_key, select_option, upload_file, history, wait_for,
paged read, and the MCP stdout guard."""

from __future__ import annotations

import asyncio
import io
import os
import sys

import pytest

import wraith
import wraith.agent as ag
import wraith.mcp as m
from tests._fakes import NodeHandle, cli_mcp_calls, mcp_browser_kwargs
from wraith.agent import AgentBrowser
from wraith.snapshot import Element, Snapshot
from wraith.uploads import UploadNotAllowedError, UploadPolicy


class _Keyboard:
    def __init__(self):
        self.pressed = []

    def press(self, key, delay=0):
        self.pressed.append((key, delay))


class _Select:
    def __init__(self, options):
        self.options, self.selected = options, None

    def evaluate(self, js):
        return self.options

    def select_option(self, option, timeout=None):
        self.selected = option


class _Page:
    url = "https://a.test/"

    def __init__(self, node=None):
        self.keyboard = _Keyboard()
        self.node = node
        self.waited = []

    def evaluate_handle(self, js, index):
        return NodeHandle(self.node)

    def wait_for_function(self, js, arg=None, timeout=None):
        self.waited.append(("text", arg, timeout))
        if arg == "never":
            raise RuntimeError("Timeout 1000ms exceeded")

    def wait_for_timeout(self, ms):
        self.waited.append(("sleep", ms))


@pytest.fixture
def browser(monkeypatch):
    def make(node=None, **kw):
        page = _Page(node)
        session = type("S", (), {"page": page, "context": None})()
        ab = AgentBrowser(session=session, **kw)
        ab.last_snapshot = Snapshot(
            page.url, "", [Element(3, "select", "combobox", "", {})]
        )
        monkeypatch.setattr(
            ag, "_await_navigation", lambda page, action, allows=None: action()
        )
        monkeypatch.setattr(AgentBrowser, "_wait_for_settle", lambda self: None)
        monkeypatch.setattr(
            AgentBrowser, "snapshot", lambda self: Snapshot(page.url, "", [])
        )
        return ab, page

    return make


def test_press_key_holds_the_key(browser):
    ab, page = browser()
    ab.press_key(" Escape ")
    ((key, delay),) = page.keyboard.pressed
    assert key == "Escape" and 35 <= delay <= 95
    with pytest.raises(ValueError, match="key name"):
        ab.press_key("  ")


def test_select_option_matches_value_or_label(browser):
    select = _Select([["us", "United States"], ["il", "Israel"]])
    ab, _ = browser(select)
    ab.select_option(3, "Israel")
    assert select.selected == "Israel"

    with pytest.raises(ValueError, match="Options: United States, Israel"):
        ab.select_option(3, "France")


def test_select_option_refuses_other_elements(browser):
    ab, _ = browser(_Select(None))
    with pytest.raises(RuntimeError, match="not a <select>"):
        ab.select_option(3, "x")


def test_wait_for_caps_the_wait_and_reports_a_timeout(browser):
    ab, page = browser()
    ab.wait_for("Done", seconds=500)
    assert page.waited == [("text", "Done", 60000)]
    with pytest.raises(TimeoutError, match="'never' did not appear within 1 s"):
        ab.wait_for("never", seconds=1)
    ab.wait_for(seconds=0.5)
    assert page.waited[-1] == ("sleep", 500)


def test_upload_is_off_without_folders(browser, tmp_path):
    ab, _ = browser()
    secret = tmp_path / "id_ed25519"
    secret.write_text("key")
    with pytest.raises(UploadNotAllowedError, match="File upload is off"):
        ab.upload_file(3, str(secret))


def test_upload_policy_keeps_files_inside_the_folders(tmp_path):
    allowed, other = tmp_path / "allowed", tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    (allowed / "cv.pdf").write_text("cv")
    (other / "key").write_text("key")
    (allowed / "link").symlink_to(other / "key")
    policy = UploadPolicy.parse(str(allowed))

    assert policy.check(allowed / "cv.pdf") == [str((allowed / "cv.pdf").resolve())]
    with pytest.raises(UploadNotAllowedError, match="outside the upload folders"):
        policy.check(str(other / "key"))
    with pytest.raises(UploadNotAllowedError, match="outside"):
        policy.check(str(allowed / "link"))  # a symlink out of the folder
    with pytest.raises(UploadNotAllowedError, match="does not exist"):
        policy.check(str(allowed / "missing.pdf"))
    with pytest.raises(UploadNotAllowedError, match="not a regular file"):
        policy.check(str(allowed))
    with pytest.raises(ValueError, match="not a directory"):
        UploadPolicy([str(allowed / "cv.pdf")])
    assert (
        UploadPolicy.parse(None) is None
        and UploadPolicy.parse(f" {os.pathsep} ") is None
    )
    assert wraith.UploadPolicy is UploadPolicy


def test_upload_file_sets_files_on_a_file_input(browser, tmp_path):
    (tmp_path / "cv.pdf").write_text("cv")

    class _FileInput:
        files = None

        def evaluate(self, js):
            return True

        def set_input_files(self, files):
            self.files = files

    node = _FileInput()
    ab, _ = browser(node, upload_dirs=[str(tmp_path)])
    ab.upload_file(3, [str(tmp_path / "cv.pdf")])
    assert node.files == [str((tmp_path / "cv.pdf").resolve())]


def test_read_comes_in_slices():
    short = m._read_slice("abc", 0, 20000)
    assert short.startswith("<untrusted-page-content") and "Characters" not in short

    text = "x" * 2500
    first = m._read_slice(text, 0, 1000)
    assert first.endswith(
        "[Characters 0-1000 of 2500. Call read(start=1000) for more.]"
    )
    last = m._read_slice(text, 2000, 1000)
    assert last.endswith("[Characters 2000-2500 of 2500.]")
    assert m._read_slice(text, 0, 10).endswith(
        "read(start=1000) for more.]"
    )  # min slice


def test_mcp_browser_gets_upload_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_UPLOAD_DIRS", None)
    monkeypatch.setenv(m.ENV_UPLOAD_DIRS, str(tmp_path))
    assert mcp_browser_kwargs(monkeypatch)["upload_dirs"] == str(tmp_path)


def test_cli_upload_dir_flag(monkeypatch):
    calls = cli_mcp_calls(monkeypatch, ["--upload-dir", "/a", "--upload-dir", "/b"])
    assert calls == [("set_upload_dirs", ["/a", "/b"]), ("main",)]


def test_mcp_tools_call_the_browser(monkeypatch):
    calls = []

    class FakeBrowser:
        def __getattr__(self, name):
            return lambda *a: (
                calls.append((name, a)) or Snapshot("https://a.test/", "", [])
            )

    monkeypatch.setattr(m, "_get_browser", lambda: FakeBrowser())
    asyncio.run(m.press_key("Escape"))
    asyncio.run(m.select_option(2, "Israel"))
    asyncio.run(m.upload_file(4, ["/tmp/a.pdf"]))
    asyncio.run(m.go_back())
    asyncio.run(m.go_forward())
    asyncio.run(m.wait_for("Done", 3))
    assert calls == [
        ("press_key", ("Escape",)),
        ("select_option", (2, "Israel")),
        ("upload_file", (4, ["/tmp/a.pdf"])),
        ("go_back", ()),
        ("go_forward", ()),
        ("wait_for", ("Done", 3)),
    ]


def test_stdout_guard_sends_text_to_stderr(monkeypatch):
    wire = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    err = io.StringIO()
    monkeypatch.setattr(sys, "stdout", wire)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(m.app, "run", lambda: print("stray"))

    m.main()

    assert err.getvalue() == "stray\n"
    assert sys.stdout.buffer is wire.buffer and wire.buffer.getvalue() == b""
