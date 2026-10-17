"""Offline tests for the Chromium storageState handoff (e.g. to agent-browser)."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

import wraith
import wraith.mcp as m
from wraith import cli
from wraith.agent import AgentBrowser
from wraith.identity import chromium_storage_state, export_storage_state
from wraith.secrets import SecretPolicyError

STATE = {
    "cookies": [
        {"name": "sid", "value": "a", "domain": "example.com", "path": "/",
         "expires": -1, "httpOnly": True, "secure": False, "sameSite": "None"},
        {"name": "xsite", "value": "b", "domain": "example.com", "path": "/",
         "expires": -1, "httpOnly": False, "secure": True, "sameSite": "None"},
        {"name": "lax", "value": "c", "domain": "example.com", "path": "/",
         "expires": -1, "httpOnly": False, "secure": False, "sameSite": "Lax"},
        {"name": "strict", "value": "d", "domain": "example.com", "path": "/",
         "expires": -1, "httpOnly": False, "secure": False, "sameSite": "Strict"},
    ],
    "origins": [{"origin": "https://example.com",
                 "localStorage": [{"name": "token", "value": "t"}]}],
}


class _Context:
    def __init__(self, state=STATE):
        self.state = state
        self.saved_path = None

    def storage_state(self, path=None):
        if path is not None:
            self.saved_path = path
            return None
        return copy.deepcopy(self.state)


class _Session:
    def __init__(self, ctx):
        self.context = ctx
        self.page = object()


def test_chromium_storage_state_sets_lax_on_insecure_none_cookies():
    original = copy.deepcopy(STATE)

    out = chromium_storage_state(STATE)

    assert [c["sameSite"] for c in out["cookies"]] == ["Lax", "None", "Lax", "Strict"]
    assert [c["secure"] for c in out["cookies"]] == [False, True, False, False]
    assert out["origins"] == STATE["origins"]
    assert STATE == original


def test_chromium_storage_state_is_exported_from_package():
    assert wraith.chromium_storage_state is chromium_storage_state
    assert wraith.export_storage_state is export_storage_state


def test_export_for_chromium_writes_converted_state(tmp_path):
    path = tmp_path / "state.json"

    assert export_storage_state(_Context(), str(path), for_chromium=True) == str(path)

    saved = json.loads(path.read_text())
    assert saved["cookies"][0]["sameSite"] == "Lax"
    assert saved["origins"] == STATE["origins"]


def test_export_default_keeps_playwright_writer():
    ctx = _Context()

    export_storage_state(ctx, "/tmp/state.json")

    assert ctx.saved_path == "/tmp/state.json"


def test_agent_browser_saves_state_for_chromium(tmp_path):
    path = tmp_path / "state.json"
    browser = AgentBrowser(session=_Session(_Context()))

    browser.save_storage_state(str(path), for_chromium=True)

    assert json.loads(path.read_text())["cookies"][0]["sameSite"] == "Lax"


def test_chromium_export_keeps_secret_taint_block(tmp_path, monkeypatch):
    monkeypatch.setattr(AgentBrowser, "secret_tainted", property(lambda self: True))
    browser = AgentBrowser(session=_Session(_Context()))

    with pytest.raises(SecretPolicyError):
        browser.save_storage_state(str(tmp_path / "state.json"), for_chromium=True)


def test_mcp_save_state_passes_for_chromium(monkeypatch):
    calls = []

    class FakeBrowser:
        def save_storage_state(self, path, **kw):
            calls.append((path, kw))
            return path

    monkeypatch.setattr(m, "_get_browser", lambda: FakeBrowser())

    result = asyncio.run(m.save_state("/tmp/state.json", for_chromium=True))

    assert calls == [("/tmp/state.json", {"for_chromium": True})]
    assert "Chromium" in result


def test_cli_borrow_saves_chromium_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    ctx = _Context()

    class FakeEngine:
        def launch(self, *a, **kw):
            return self

        def __enter__(self):
            return _Session(ctx)

        def __exit__(self, *exc):
            return False

    class FakePage:
        url = "https://example.com/"

        def goto(self, url, **kw):
            pass

    import wraith.identity as identity

    modules = {"engine": FakeEngine(), "identity": identity}
    monkeypatch.setattr(cli, "_lazy", lambda name: modules[name])
    monkeypatch.setattr(cli, "_resolve_profile", lambda identity, profile: tmp_path)
    monkeypatch.setattr(identity, "extract_cookies", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "_page_of", lambda session, context=None: FakePage())
    monkeypatch.setattr(cli, "_resolve_proxy", lambda args: None)

    rc = cli.main([
        "borrow", "https://example.com/", "--headless", "--no-wait",
        "--save-state", str(path), "--for-chromium",
    ])

    assert rc == 0
    assert json.loads(path.read_text())["cookies"][0]["sameSite"] == "Lax"


def test_cli_for_chromium_requires_save_state():
    with pytest.raises(SystemExit):
        cli.main(["borrow", "https://example.com/", "--for-chromium"])
