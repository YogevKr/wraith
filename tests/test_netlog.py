"""Offline tests for the network log and its agent and MCP tools."""

from __future__ import annotations

import asyncio

import pytest

import wraith.mcp as m
from wraith.agent import AgentBrowser
from wraith.netlog import NetworkLog, describe
from wraith.secrets import SecretPolicyError


class _Context:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def emit(self, event, arg):
        self.handlers[event](arg)


class _Request:
    def __init__(self, url, method="GET", resource_type="fetch", failure=None):
        self.url, self.method, self.resource_type, self.failure = (
            url,
            method,
            resource_type,
            failure,
        )


class _Response:
    def __init__(
        self, request, status=200, content_type="application/json", body=b'{"a": 1}'
    ):
        self.request, self.status, self._body = request, status, body
        self.headers = {"content-type": content_type}

    def body(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def log():
    context, network = _Context(), NetworkLog(limit=3)
    network.install(context)
    return context, network


def _visit(context, request, response=None):
    context.emit("request", request)
    if response is not None:
        context.emit("response", response)
        context.emit("requestfinished", request)


def test_log_records_requests_responses_and_failures(log):
    context, network = log
    api = _Request("https://a.test/api/items?page=2&token=abc")
    _visit(context, api, _Response(api))
    blocked = _Request("https://evil.test/pixel.png", resource_type="image")
    context.emit("request", blocked)
    blocked.failure = "net::ERR_BLOCKED_BY_CLIENT"
    context.emit("requestfailed", blocked)

    first, second = network.entries()
    assert (first.id, first.status, first.content_type) == (1, 200, "application/json")
    assert second.failure == "net::ERR_BLOCKED_BY_CLIENT" and second.status is None
    assert (
        describe(first.to_dict())
        == "#1 GET 200 fetch https://a.test/api/items?page=2&token=abc  [application/json]"
    )
    assert first.to_dict(strip_query=True)["url"] == "https://a.test/api/items"
    assert describe(second.to_dict()).endswith("FAILED: net::ERR_BLOCKED_BY_CLIENT")
    assert network.body(1) == '{"a": 1}'
    with pytest.raises(LookupError, match="no response body"):
        network.body(2)


def test_log_filters_and_keeps_only_the_last_entries(log):
    context, network = log
    for n in range(5):
        req = _Request(
            f"https://a.test/{'api' if n % 2 else 'img'}/{n}",
            resource_type="xhr" if n % 2 else "image",
        )
        _visit(context, req, _Response(req))

    assert [e.id for e in network.entries()] == [3, 4, 5]  # limit=3
    assert [e.id for e in network.entries(contains="/API/")] == [4]
    assert [e.id for e in network.entries(resource_type="image", limit=1)] == [5]
    with pytest.raises(KeyError, match="#1 is not in the network log"):
        network.entry(1)


def test_binary_and_evicted_bodies(log):
    context, network = log
    image = _Request("https://a.test/logo.png", resource_type="image")
    _visit(
        context, image, _Response(image, content_type="image/png", body=b"\x89PNG....")
    )
    gone = _Request("https://a.test/old")
    _visit(
        context,
        gone,
        _Response(gone, body=RuntimeError("No resource with given identifier")),
    )

    assert network.body(1) == "(binary body: 8 bytes, image/png)"
    with pytest.raises(LookupError, match="no longer has the body"):
        network.body(2)


@pytest.fixture
def browser(log):
    context, network = log
    page = type("P", (), {"url": "https://a.test/"})()
    ab = AgentBrowser(session=type("S", (), {"page": page, "context": context})())
    ab.network_log = network
    ab.session  # installs the agent's own log on the context too
    api = _Request("https://a.test/api?token=abc")
    _visit(context, api, _Response(api))
    return ab


def test_agent_hides_secrets_after_a_secret_fill(browser):
    assert browser.network()[0]["url"] == "https://a.test/api?token=abc"
    assert browser.network_response(1) == '{"a": 1}'

    browser._secret_state.tainted = True
    assert browser.network()[0]["url"] == "https://a.test/api"
    with pytest.raises(SecretPolicyError, match="blocked after a secret fill"):
        browser.network_response(1)
    assert browser.network_response(1, allow_secret_tainted=True) == '{"a": 1}'


def test_mcp_network_tools(browser, monkeypatch):
    monkeypatch.delenv(m.ENV_CONTENT_BOUNDARIES, raising=False)
    monkeypatch.setattr(m, "_get_browser", lambda: browser)

    listing = asyncio.run(m.network_requests(contains="api"))
    assert (
        listing.startswith("<untrusted-page-content")
        and "#1 GET 200 fetch https://a.test/api?token=abc" in listing
    )
    assert "(no matching requests)" in asyncio.run(
        m.network_requests(contains="nothing")
    )

    browser.network_log.entry(1).response._body = b"x" * 2500
    page = asyncio.run(m.network_response(1, max_chars=1000))
    assert page.endswith("Call network_response(request_id=1, start=1000) for more.]")
