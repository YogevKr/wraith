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
        self.focused = None

    def press(self, key, delay=0):
        self.pressed.append((key, delay))
        if self.focused is not None:
            self.focused.key(key)


class _Select:
    """A <select> with the browser's type-ahead: typed text picks a label."""

    def __init__(self, options, value=None, multiple=False, keyboard=None):
        self.options, self.value, self.multiple = options, value, multiple
        self.keyboard, self.typed, self.selected = keyboard, "", None

    def evaluate(self, js):
        if js == ag._SELECT_OPTIONS_JS:
            if self.options is None:
                return None
            state = {"value": self.value, "multiple": self.multiple, "disabled": False}
            return dict(state, options=self.options)
        return self.value  # "el => el.value"

    def focus(self):
        self.typed = ""
        self.keyboard.focused = self

    def key(self, char):
        if char == "é":
            raise RuntimeError('Keyboard.press: Unknown key: "é"')
        self.typed += char
        match = next(
            (
                v
                for v, label in self.options
                if label.lower().startswith(self.typed.lower())
            ),
            None,
        )
        if match is not None:
            self.value = match

    def select_option(self, value=None, timeout=None):
        self.selected = self.value = value


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


COUNTRIES = [["us", "United States"], ["uk", "United Kingdom"], ["il", "Israel"]]


@pytest.fixture
def select_list(browser, monkeypatch):
    monkeypatch.setattr(ag.time, "sleep", lambda seconds: None)

    def make(options, **kw):
        keyboard = _Keyboard()
        select = _Select(options, keyboard=keyboard, **kw)
        ab, page = browser(select)
        page.keyboard = keyboard
        return ab, select, keyboard

    return make


def test_select_option_types_the_label_like_a_keyboard_user(select_list, monkeypatch):
    ab, select, keyboard = select_list(COUNTRIES, value="us")
    ab.select_option(3, "uk")  # by value; the label is typed
    assert "".join(key for key, _ in keyboard.pressed) == "United Kingdom"
    assert all(35 <= hold <= 95 for _, hold in keyboard.pressed)
    assert select.value == "uk" and select.selected is None  # no untrusted fallback

    keyboard.pressed.clear()
    ab.select_option(3, "United Kingdom")  # already chosen
    assert keyboard.pressed == []

    sleeps = []
    monkeypatch.setattr(ag.time, "sleep", sleeps.append)
    ab.select_option(3, "Israel")  # right after typing: wait for the reset
    assert sleeps[0] > 1 and select.value == "il"

    with pytest.raises(
        ValueError, match="Options: United States, United Kingdom, Israel"
    ):
        ab.select_option(3, "France")


@pytest.mark.parametrize(
    "options, kw, wanted",
    [
        ([["a", "Café"], ["b", "Bar"]], {}, "a"),  # a character with no key
        ([["a", "Same"], ["b", "Same"]], {"value": "x"}, "b"),  # type-ahead picks "a"
        (COUNTRIES, {"multiple": True}, "il"),  # a multiple list is not typed into
    ],
)
def test_select_option_falls_back_when_typing_cannot_reach_it(
    select_list, options, kw, wanted
):
    ab, select, keyboard = select_list(options, **kw)
    ab.select_option(3, wanted)
    assert select.selected == wanted
    if kw.get("multiple"):
        assert keyboard.pressed == []


def test_select_option_refuses_other_elements(select_list):
    ab, _, _ = select_list(None)
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
            assert js == ag._UPLOAD_TARGET_JS
            return "file"

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


@pytest.mark.parametrize("kind", ["a link", "a text field"])
def test_upload_refuses_links_and_text_fields_before_clicking(browser, tmp_path, kind):
    (tmp_path / "cv.pdf").write_text("cv")
    clicks = []

    class _Target:
        def evaluate(self, js):
            return kind

        def click(self, **kw):
            clicks.append(kw)

    ab, _ = browser(_Target(), upload_dirs=[str(tmp_path)])
    with pytest.raises(RuntimeError, match=f"is {kind}, not a file input"):
        ab.upload_file(3, str(tmp_path / "cv.pdf"))
    assert clicks == []


def test_upload_trigger_that_opens_no_chooser_says_so(browser, tmp_path, monkeypatch):
    (tmp_path / "cv.pdf").write_text("cv")

    class _Button:
        def evaluate(self, js):
            return "trigger"

    ab, page = browser(_Button(), upload_dirs=[str(tmp_path)])

    class _NoChooser:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            raise TimeoutError('Timeout 5000ms exceeded while waiting for event "filechooser"')

    page.expect_file_chooser = lambda timeout=None: _NoChooser()
    monkeypatch.setattr(AgentBrowser, "_pointer_click", lambda self, index: None)
    with pytest.raises(RuntimeError, match=r"opened no file chooser within 5 s"):
        ab.upload_file(3, str(tmp_path / "cv.pdf"))
