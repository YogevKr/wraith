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


# --------------------------------------------------------------------------- #
# Failed navigation (no main-frame response)
# --------------------------------------------------------------------------- #
class _FailingPage(_FakePage):
    """goto() raises like Playwright; optionally after one response arrived."""

    def __init__(self, error, *, respond_first=None):
        super().__init__(200, "https://old.example/", content="old page " * 100)
        self._error = error
        self._respond_first = respond_first

    def goto(self, url, **kw):
        self.goto_calls.append(url)
        if self._respond_first is not None:
            for cb in self._listeners.get("response", []):
                cb(_FakeResponse(self._respond_first, url))
        raise self._error


def test_navigation_without_response_raises_with_cause():
    from wraith.engine import NavigationFailedError

    error = RuntimeError("Page.goto: NS_ERROR_UNKNOWN_HOST\nCall log:\n  - navigating")
    sess = _FakeSession(_FailingPage(error), _FakeContext([[]]))

    with pytest.raises(NavigationFailedError, match="NS_ERROR_UNKNOWN_HOST") as info:
        clear_challenge(URL, session=sess, timeout=5.0, settle=0.0)

    assert URL in str(info.value)
    assert "Call log" not in str(info.value)
    assert issubclass(NavigationFailedError, WraithEngineError)
    assert wraith.NavigationFailedError is NavigationFailedError


def test_aborted_navigation_after_response_keeps_polling():
    error = RuntimeError("Page.goto: NS_BINDING_ABORTED")
    sess = _FakeSession(_FailingPage(error, respond_first=200), _FakeContext([[]]))

    assert clear_challenge(URL, session=sess, timeout=5.0, settle=0.0) is sess


# --------------------------------------------------------------------------- #
# A hold that clears while the loop still holds the old page's content
# --------------------------------------------------------------------------- #
DENIED = (
    "<title>Access to this page has been denied</title>"
    "<p>Press &amp; Hold to confirm you are a human (and not a bot).</p>"
    '<div id="px-captcha"></div>'
)


def test_a_hold_that_clears_mid_poll_is_not_called_a_hard_block(monkeypatch):
    """CI flake: the cleared page committed between the content read and the
    hold check, so the stale denied content was reported as a hard block."""
    from wraith import _press_hold

    sess = _make_session(403, URL, cookies_sequence=[[]], content=DENIED)
    page = sess.page
    page.title = lambda: "Access to this page has been denied" if "denied" in page._content else ""
    polls = []

    def poll(self, page_, html):
        polls.append(html)
        if len(polls) == 1:
            self.seen = True
            return True  # the hold happens on this poll
        if len(polls) == 2:
            # The cleared page answers while this poll runs.
            page._content = "<h1>Verified catalog</h1>" + "<p>Product</p>" * 40
            cleared = _FakeResponse(200, URL + "cleared")
            cleared.request = type(
                "Req", (), {"frame": None, "is_navigation_request": lambda self: True}
            )()
            for cb in page._listeners.get("response", []):
                cb(cleared)
        return False

    monkeypatch.setattr(_press_hold.PressHoldHandler, "poll", poll)

    assert clear_challenge(URL, session=sess, timeout=5.0, settle=0.0) is sess
    assert polls[1] == DENIED  # the stale read that used to raise


def _hold_then(page, on_poll):
    """A poll that holds on the first call, then runs ``on_poll(n)``."""
    polls = []

    def poll(self, page_, html):
        polls.append(html)
        if len(polls) == 1:
            self.seen = True
            return True
        on_poll(len(polls))
        return False

    return poll, polls


def _navigation_response(page, url):
    response = _FakeResponse(200, url)
    response.request = type(
        "Req", (), {"frame": None, "is_navigation_request": lambda self: True}
    )()
    for cb in page._listeners.get("response", []):
        cb(response)


def test_a_cleared_page_that_commits_before_its_response_is_not_a_hard_block(
    monkeypatch,
):
    """CI flake: Firefox delivered the cleared page's commit before the
    redirect's response event, so the stale denied content looked fresh."""
    from wraith import _press_hold

    sess = _make_session(403, URL, cookies_sequence=[[]], content=DENIED)
    page = sess.page
    page.title = lambda: (
        "Access to this page has been denied" if "denied" in page._content else ""
    )

    def on_poll(n):
        if n == 2:  # the cleared page commits; its response is not seen yet
            page._content = "<h1>Verified catalog</h1>" + "<p>Product</p>" * 40
        if n == 3:
            _navigation_response(page, URL + "cleared")

    poll, polls = _hold_then(page, on_poll)
    monkeypatch.setattr(_press_hold.PressHoldHandler, "poll", poll)

    assert clear_challenge(URL, session=sess, timeout=5.0, settle=0.0) is sess
    assert polls[1] == DENIED  # the stale read that used to raise


def test_denied_text_after_the_settle_window_is_still_a_hard_block(monkeypatch):
    from wraith import _press_hold, engine

    monkeypatch.setattr(engine, "_HOLD_SETTLE_S", 0.0)
    sess = _make_session(403, URL, cookies_sequence=[[]], content=DENIED)
    sess.page.title = lambda: "Access to this page has been denied"
    poll, polls = _hold_then(sess.page, lambda n: None)
    monkeypatch.setattr(_press_hold.PressHoldHandler, "poll", poll)

    with pytest.raises(WaapHardBlockError, match="PerimeterX"):
        clear_challenge(URL, session=sess, timeout=5.0, settle=0.0)
    assert len(polls) == 2  # not while holding; on the first poll after it


# --------------------------------------------------------------------------- #
# A 200 challenge wall is not real content yet
# --------------------------------------------------------------------------- #
_CF_CHALLENGE = (
    "<html><head><title>Just a moment...</title></head><body>"
    "challenge-platform " + "x" * 500 + "</body></html>"
)


def test_a_200_challenge_page_is_not_accepted_as_cleared():
    sess = _make_session(200, URL, cookies_sequence=[[]], content=_CF_CHALLENGE)
    with pytest.raises(WaapChallengeTimeout):
        clear_challenge(URL, session=sess, timeout=0.2, settle=0.0)


def test_a_200_challenge_page_clears_once_the_cookie_arrives():
    sess = _make_session(
        200,
        URL,
        cookies_sequence=[[], [{"name": "cf_clearance", "value": "v"}]],
        content=_CF_CHALLENGE,
    )
    assert clear_challenge(URL, session=sess, timeout=5.0, settle=0.0) is sess


def test_a_plain_200_page_still_returns_without_a_cookie():
    sess = _make_session(200, URL, cookies_sequence=[[]], content="y" * 500)
    assert clear_challenge(URL, session=sess, timeout=5.0, settle=0.0) is sess


def test_navigate_false_polls_the_current_page():
    sess = _make_session(200, URL, cookies_sequence=[[]], content="y" * 500)
    returned = clear_challenge(
        URL, session=sess, timeout=5.0, settle=0.0, navigate=False
    )
    assert returned is sess
    assert sess.page.goto_calls == []


# --------------------------------------------------------------------------- #
# require_clearance: only a valid clearance cookie counts
# --------------------------------------------------------------------------- #
def test_require_clearance_rejects_clean_content_without_a_cookie():
    sess = _make_session(200, URL, cookies_sequence=[[]], content="y" * 500)
    with pytest.raises(WaapChallengeTimeout):
        clear_challenge(
            URL, session=sess, timeout=0.2, settle=0.0, require_clearance=True
        )


def test_require_clearance_accepts_a_valid_cookie():
    sess = _make_session(
        200, URL, cookies_sequence=[[{"name": "_abck", "value": "a~0~b"}]]
    )
    assert (
        clear_challenge(
            URL, session=sess, timeout=5.0, settle=0.0, require_clearance=True
        )
        is sess
    )


def test_require_clearance_rejects_an_unsolved_abck():
    # _abck present but still '~-1~' is not valid (the Akamai sentinel).
    sess = _make_session(
        200,
        URL,
        cookies_sequence=[[{"name": "_abck", "value": "a~-1~b"}]],
        content="y" * 500,
    )
    with pytest.raises(WaapChallengeTimeout):
        clear_challenge(
            URL, session=sess, timeout=0.2, settle=0.0, require_clearance=True
        )
