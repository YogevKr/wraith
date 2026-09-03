"""Offline regression tests: ``engine.launch(proxy=<url string>)`` must reach the
engine as Playwright's ``{"server", "username", "password"}`` dict.

Bug this covers: Camoufox's ``proxy`` option is a dict; handing it the URL
string that ``ProxyPool`` / the provider classes emit (and that
``clear_challenge`` rotation passes to ``launch``) failed inside Camoufox with
``AttributeError: 'str' object has no attribute 'get'``. No browser is
launched here — the engine-specific launcher is stubbed to capture kwargs.
"""

from __future__ import annotations

import pytest

from wraith import engine


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    def fake_camoufox(**kw):
        calls.append(kw)
        return "session-sentinel"

    def fake_chromium(**kw):
        calls.append(kw)
        return "session-sentinel"

    monkeypatch.setattr(engine, "_launch_camoufox", fake_camoufox)
    monkeypatch.setattr(engine, "_launch_chromium", fake_chromium)
    return calls


def test_string_proxy_becomes_dict_for_camoufox(captured):
    engine.launch(engine="camoufox", proxy="http://user_ab12,type_mobile:pw@portal.anyip.io:1080")
    assert captured[0]["extra"]["proxy"] == {
        "server": "http://portal.anyip.io:1080",
        "username": "user_ab12,type_mobile",
        "password": "pw",
    }


def test_string_proxy_becomes_dict_for_chromium(captured):
    engine.launch(engine="chromium", proxy="acct__cr.il;sessid.x:pw@gw.dataimpulse.com:823")
    assert captured[0]["extra"]["proxy"] == {
        "server": "http://gw.dataimpulse.com:823",
        "username": "acct__cr.il;sessid.x",
        "password": "pw",
    }


def test_dict_proxy_passes_through(captured):
    d = {"server": "socks5://h:1", "username": "u", "password": "p"}
    engine.launch(engine="camoufox", proxy=d)
    assert captured[0]["extra"]["proxy"] == d


def test_none_proxy_stays_none_and_absent_stays_absent(captured):
    engine.launch(engine="camoufox", proxy=None)
    assert captured[0]["extra"]["proxy"] is None
    engine.launch(engine="camoufox")
    assert "proxy" not in captured[1]["extra"]
