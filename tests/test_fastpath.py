"""Offline tests for the no-browser TLS-impersonation fast path.

Pure logic + request-assembly (curl_cffi monkeypatched) — no network.
"""
from __future__ import annotations

import http.cookiejar

import pytest

from wraith import detect, identity


# --------------------------------------------------------------------------- #
# identity.to_cookiejar
# --------------------------------------------------------------------------- #

def test_to_cookiejar_from_cookie_objects():
    cks = [
        identity.Cookie(name="a", value="1", domain=".example.com", path="/", secure=True),
        identity.Cookie(name="b", value="2", domain="x.example.com"),
    ]
    jar = identity.to_cookiejar(cks)
    assert isinstance(jar, http.cookiejar.CookieJar)
    assert {c.name: c.value for c in jar} == {"a": "1", "b": "2"}
    a = next(c for c in jar if c.name == "a")
    assert a.secure is True and a.domain == ".example.com"


def test_to_cookiejar_from_playwright_dicts():
    jar = identity.to_cookiejar(
        [{"name": "s", "value": "v", "domain": "example.com", "path": "/", "httpOnly": True}]
    )
    c = next(iter(jar))
    assert c.name == "s" and c.value == "v"
    assert c.has_nonstandard_attr("HttpOnly")


def test_to_cookiejar_skips_nameless():
    jar = identity.to_cookiejar([{"value": "v", "domain": "x"}])
    assert len(list(jar)) == 0


# --------------------------------------------------------------------------- #
# detect.classify_response
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "status,headers,body,state,vendor",
    [
        (200, {}, "", "ok", None),
        (429, {}, "", "rate_limited", None),
        (474, {}, "", "rate_limited", "reblaze"),
        (492, {}, "", "blocked", "reblaze"),
        (247, {}, "", "challenge", "reblaze"),
        (403, {"cf-ray": "x", "server": "cloudflare"}, "Just a moment... challenge-platform", "challenge", "cloudflare"),
        (403, {"server": "cloudflare", "cf-ray": "y"}, "error 1020 you have been blocked", "blocked", "cloudflare"),
        (403, {"set-cookie": "datadome=xyz"}, "captcha-delivery", "challenge", "datadome"),
        (401, {}, "", "auth_required", None),
        (503, {}, "", "server_error", None),
    ],
)
def test_classify_response(status, headers, body, state, vendor):
    sig = detect.classify_response(status, headers, body)
    assert sig.state == state
    if vendor:
        assert sig.vendor == vendor
    assert sig.ok == (state == "ok")


# --------------------------------------------------------------------------- #
# fastpath
# --------------------------------------------------------------------------- #

def test_ua_to_impersonate():
    from wraith import fastpath

    assert "firefox" in fastpath.ua_to_impersonate("Mozilla/5.0 (X11) Gecko/20100101 Firefox/140")
    assert "chrome" in fastpath.ua_to_impersonate(
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
    )
    assert "safari" in fastpath.ua_to_impersonate(
        "Mozilla/5.0 (Macintosh) AppleWebKit/605 Version/17 Safari/605.1.15"
    )
    assert fastpath.ua_to_impersonate("") == fastpath._DEFAULT_IMPERSONATE


def test_fetch_assembles_session_headers_and_impersonate(monkeypatch):
    from wraith import fastpath

    captured: dict = {}

    class FakeResp:
        status_code = 200
        text = "ok"
        headers: dict = {}

    import curl_cffi.requests as ccr

    def fake_request(method, url, **kw):
        captured.update(method=method, url=url, **kw)
        return FakeResp()

    monkeypatch.setattr(ccr, "request", fake_request)

    class Sess:
        authorization = "Bearer T"
        cookie = "a=1; b=2"
        user_agent = "Mozilla/5.0 (X11) Gecko/20100101 Firefox/140"

    resp = fastpath.fetch("https://t.example/api", session=Sess(), proxy="http://u:p@gw:1")
    assert resp.status_code == 200
    assert captured["headers"]["Authorization"] == "Bearer T"
    assert captured["headers"]["Cookie"] == "a=1; b=2"
    assert "firefox" in captured["impersonate"]
    assert captured["proxies"] == {"http": "http://u:p@gw:1", "https": "http://u:p@gw:1"}


def test_fetch_explicit_impersonate_overrides(monkeypatch):
    from wraith import fastpath

    captured: dict = {}
    import curl_cffi.requests as ccr

    class FakeResp:
        status_code = 200
        text = ""
        headers: dict = {}

    monkeypatch.setattr(ccr, "request", lambda m, u, **kw: (captured.update(kw) or FakeResp()))
    fastpath.fetch("https://t.example", impersonate="chrome146")
    assert captured["impersonate"] == "chrome146"


def test_classify_uses_detect():
    from wraith import fastpath

    class R:
        status_code = 403
        text = "error 1020 you have been blocked"
        headers = {"server": "cloudflare", "cf-ray": "z"}

    sig = fastpath.classify(R())
    assert sig.state == "blocked" and sig.vendor == "cloudflare"


def test_wraith_reexports_fastpath_symbols():
    import wraith

    for name in ("fetch", "replay", "to_cookiejar", "classify_response"):
        assert hasattr(wraith, name), f"wraith.{name} not exported"
