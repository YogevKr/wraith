"""Offline tests for Batch 4: MCP snapshot control + tabs + storageState."""
from __future__ import annotations

import wraith.agent as ag
from wraith.agent import AgentBrowser
from wraith.snapshot import Element, Snapshot


def test_render_full_vs_compact():
    from wraith import mcp

    snap = Snapshot("http://x", "T", [Element(0, "a", "link", "Home", {"href": "/"})], changed="url changed")
    full = mcp._render(snap, True)
    assert "[0]" in full and "URL: http://x" in full
    compact = mcp._render(snap, False)
    assert "[0]" not in compact
    assert "1 interactive elements" in compact and "url changed" in compact


class _FakePage:
    def __init__(self, url="about:blank", title="t"):
        self._url = url
        self._title = title
        self._closed = False
        self.fronted = False

    @property
    def url(self):
        return self._url

    def title(self):
        return self._title

    def is_closed(self):
        return self._closed

    def bring_to_front(self):
        self.fronted = True

    def close(self):
        self._closed = True

    def goto(self, u, **k):
        self._url = u


class _FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)
        self.saved = None

    def new_page(self):
        p = _FakePage("about:blank", "new")
        self.pages.append(p)
        return p

    def storage_state(self, path=None):
        self.saved = path


class _FakeSession:
    def __init__(self, ctx):
        self.context = ctx
        self.page = ctx.pages[0]


def _agent(ctx):
    return AgentBrowser(session=_FakeSession(ctx))


def test_tabs_list_and_select(monkeypatch):
    p0, p1 = _FakePage("http://a", "A"), _FakePage("http://b", "B")
    ctx = _FakeContext([p0, p1])
    a = _agent(ctx)
    tabs = a.tabs()
    assert len(tabs) == 2
    assert tabs[0]["active"] is True and tabs[1]["active"] is False
    monkeypatch.setattr(ag, "take_snapshot", lambda page, **kw: Snapshot(page.url, "", []))
    a.select_tab(1)
    assert a.page is p1 and p1.fronted is True


def test_new_and_close_tab(monkeypatch):
    monkeypatch.setattr(ag, "take_snapshot", lambda page, **kw: Snapshot(getattr(page, "url", ""), "", []))
    p0 = _FakePage("http://a", "A")
    ctx = _FakeContext([p0])
    a = _agent(ctx)
    monkeypatch.setattr(a, "_wait_for_settle", lambda: None)
    a.new_tab("http://c")
    assert len(ctx.pages) == 2 and a.page.url == "http://c"
    a.close_tab(1)
    assert ctx.pages[1].is_closed() is True


def test_save_storage_state():
    ctx = _FakeContext([_FakePage()])
    a = _agent(ctx)
    out = a.save_storage_state("/tmp/state.json")
    assert out == "/tmp/state.json" and ctx.saved == "/tmp/state.json"
