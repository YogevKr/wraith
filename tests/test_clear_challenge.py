"""Offline unit tests for the WAAP challenge-clearing helper.

These run entirely without a real browser. They use a tiny fake
``Session``/``context``/``page`` (with a stubbed top-level response) injected via
the ``session=`` parameter of :func:`wraith.clear_challenge`, so no engine is
ever launched.

Covered:
* the three new exceptions exist, subclass ``WraithEngineError``, and are
  importable from the top-level ``wraith`` package;
* ``clear_challenge`` is importable, callable, and in ``wraith.__all__``;
* an HTTP 474 top-level status raises ``WaapRateLimitedError``;
* an HTTP 492 top-level status raises ``WaapHardBlockError``;
* a clearance cookie appearing returns the (same) injected session;
* a caller-supplied session is NOT closed by ``clear_challenge``.
"""

from __future__ import annotations

import pytest

import wraith
from wraith.engine import (
    WraithEngineError,
    WaapRateLimitedError,
    WaapHardBlockError,
    WaapChallengeTimeout,
    clear_challenge,
)


# --------------------------------------------------------------------------- #
# Fakes (no real browser)
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status, url):
        self.status = status
        self.url = url


class _FakePage:
    """Minimal Page: records goto() and returns a stubbed response status."""

    def __init__(self, status, url, content="x" * 500):
        self._status = status
        self._url = url
        self._content = content
        self.goto_calls = []
        self._listeners = {}

    def on(self, event, cb):  # noqa: D401 - listener registration
        self._listeners.setdefault(event, []).append(cb)

    def goto(self, url, **kw):
        self.goto_calls.append(url)
        resp = _FakeResponse(self._status, url)
        # Fire the response listener too, mimicking Playwright behaviour.
        for cb in self._listeners.get("response", []):
            cb(resp)
        return resp

    def content(self):
        return self._content

    def wait_for_timeout(self, ms):
        # No real waiting in unit tests.
        return None


class _FakeContext:
    """Minimal BrowserContext: cookies() returns a caller-controlled list."""

    def __init__(self, cookies_sequence):
        # cookies_sequence: a list of cookie-lists to return on successive calls;
        # the last entry is repeated once exhausted.
        self._seq = list(cookies_sequence)
        self.cookies_calls = 0

    def cookies(self):
        self.cookies_calls += 1
        if not self._seq:
            return []
        if len(self._seq) == 1:
            return self._seq[0]
        return self._seq.pop(0)


class _FakeSession:
    def __init__(self, page, context):
        self.page = page
        self.context = context
        self.closed = False

    def close(self):
        self.closed = True


def _make_session(status, url, cookies_sequence, content="x" * 500):
    page = _FakePage(status, url, content=content)
    context = _FakeContext(cookies_sequence)
    return _FakeSession(page, context)


# --------------------------------------------------------------------------- #
# Exception surface
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "exc",
    [WaapRateLimitedError, WaapHardBlockError, WaapChallengeTimeout],
)
def test_exceptions_subclass_base(exc):
    assert issubclass(exc, WraithEngineError)


@pytest.mark.parametrize(
    "name",
    [
        "WaapRateLimitedError",
        "WaapHardBlockError",
        "WaapChallengeTimeout",
        "clear_challenge",
    ],
)
def test_symbols_exported_from_wraith(name):
    assert hasattr(wraith, name), f"wraith.{name} is missing"
    assert name in wraith.__all__, f"{name} not in wraith.__all__"


def test_clear_challenge_is_callable():
    assert callable(wraith.clear_challenge)
    assert callable(clear_challenge)


def test_exception_identity_matches_reexport():
    """The re-exported names must be the very same classes (no shadow copies)."""
    assert wraith.WaapRateLimitedError is WaapRateLimitedError
    assert wraith.WaapHardBlockError is WaapHardBlockError
    assert wraith.WaapChallengeTimeout is WaapChallengeTimeout
    assert wraith.clear_challenge is clear_challenge


# --------------------------------------------------------------------------- #
# Status-tier handling (474/481 -> rate limit, 492 -> hard block)
# --------------------------------------------------------------------------- #
URL = "https://www.example.com/"


@pytest.mark.parametrize("status", [474, 481])
def test_rate_limit_status_raises(status):
    sess = _make_session(status, URL, cookies_sequence=[[]])
    with pytest.raises(WaapRateLimitedError):
        clear_challenge(URL, session=sess)
    # Caller-supplied session must NOT be closed by clear_challenge.
    assert sess.closed is False


def test_hard_block_status_raises():
    sess = _make_session(492, URL, cookies_sequence=[[]])
    with pytest.raises(WaapHardBlockError):
        clear_challenge(URL, session=sess)
    assert sess.closed is False


# --------------------------------------------------------------------------- #
# Clearance-cookie success path
# --------------------------------------------------------------------------- #
def test_clearance_cookie_returns_session():
    """A 247-style flow: first cookies() empty, then waap_id appears."""
    sess = _make_session(
        247,
        URL,
        cookies_sequence=[
            [],  # first poll: no clearance yet (challenge solving)
            [{"name": "waap_id", "value": "abc"}],  # then it appears
        ],
    )
    returned = clear_challenge(URL, session=sess, timeout=5.0)
    assert returned is sess
    assert sess.closed is False
    assert sess.page.goto_calls == [URL]


def test_custom_clearance_cookie_name():
    sess = _make_session(
        200,
        URL,
        cookies_sequence=[[{"name": "my_pass", "value": "1"}]],
    )
    returned = clear_challenge(
        URL, session=sess, timeout=5.0, clearance_cookies=["my_pass"]
    )
    assert returned is sess


# --------------------------------------------------------------------------- #
# Non-WAAP / already-cleared success path (clean 200, no clearance cookie)
# --------------------------------------------------------------------------- #
def test_non_waap_clean_200_returns_session():
    sess = _make_session(200, URL, cookies_sequence=[[]])
    returned = clear_challenge(URL, session=sess, timeout=5.0, settle=0.0)
    assert returned is sess
    assert sess.closed is False


# --------------------------------------------------------------------------- #
# Timeout path
# --------------------------------------------------------------------------- #
def test_timeout_when_no_cookie_and_no_clean_content():
    # status None-ish via a non-2xx that never yields a clearance cookie and
    # whose body is too small to count as "real content".
    sess = _make_session(247, URL, cookies_sequence=[[]], content="tiny")
    with pytest.raises(WaapChallengeTimeout):
        clear_challenge(URL, session=sess, timeout=0.2, settle=10.0)
    assert sess.closed is False
