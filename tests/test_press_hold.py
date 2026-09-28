"""Offline checks for HUMAN challenge input and verified clearance."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_clear_challenge import _FakeContext, _FakePage, _FakeResponse, _FakeSession
from wraith import detect, engine
from wraith._press_hold import (
    _BUTTON,
    hold_button,
    inspect_press_hold,
    is_press_hold_challenge,
)

URL = "https://www.example.com/"
CHALLENGE = """<html><body><h1>iHerb</h1>
<p>Press &amp; Hold to confirm you are a human (and not a bot).</p>
<button>Press &amp; Hold</button>
<footer>Reference ID synthetic-reference-for-offline-test</footer>
</body></html>"""
CLEARED = "<html><body>" + "Product catalog " * 30 + "</body></html>"


def _frame(*, widget=False, prompt=False, buttons=(), text_buttons=(), hosts=()):
    return SimpleNamespace(
        locator=lambda _: SimpleNamespace(
            count=lambda: int(widget), all=lambda: list(hosts)
        ),
        get_by_role=lambda *args, **kwargs: SimpleNamespace(all=lambda: list(buttons)),
        get_by_text=lambda pattern: SimpleNamespace(
            all=lambda: (
                list(text_buttons)
                if pattern is _BUTTON
                else ([SimpleNamespace(is_visible=lambda: True)] if prompt else [])
            )
        ),
    )


@pytest.mark.parametrize(
    "label", ["Press &amp; Hold", "Press and Hold", "PRESS &#38; HOLD"]
)
def test_markup_recognizes_hold_variants(label):
    assert is_press_hold_challenge(CHALLENGE.replace("Press &amp; Hold", label))


@pytest.mark.parametrize("status", [200, 403])
def test_http_challenge_is_not_success_or_hard_block(status):
    result = detect.classify_response(status, body=CHALLENGE)
    assert (result.state, result.vendor) == ("challenge", "perimeterx")
    assert not result.ok


@pytest.mark.parametrize(
    "html",
    [
        "<button>Press &amp; Hold to record</button>",
        '<script>const label = "Press & Hold to confirm you are a human";</script>',
        CLEARED,
    ],
)
def test_markup_ignores_normal_controls_and_script_strings(html):
    assert not is_press_hold_challenge(html)
    assert detect.classify_response(200, body=html).ok


def test_interactive_denial_can_clear_but_hard_blocks_remain():
    # Raw markup cannot prove visibility. Only the engine may exempt a visible gate.
    assert detect.is_blocked(CHALLENGE, "Access to this page has been denied")
    assert detect.is_blocked("Access to this page has been denied")
    assert detect.is_blocked(CHALLENGE, "Error 1020")
    assert detect.classify_response(429, body=CHALLENGE).state == "rate_limited"


def test_probe_finds_button_in_child_frame():
    hidden = Mock(is_visible=Mock(return_value=False))
    button = Mock(is_visible=Mock(return_value=True))
    parent = _frame(prompt=True)
    child = _frame(buttons=[hidden, button])
    child.parent_frame = parent
    page = SimpleNamespace(frames=[parent, child])
    active, target = inspect_press_hold(page, CHALLENGE)
    assert active and target is button


def test_probe_handles_unlabelled_button_text():
    button = Mock(is_visible=Mock(return_value=True))
    page = SimpleNamespace(frames=[_frame(widget=True, text_buttons=[button])])
    assert inspect_press_hold(page, CHALLENGE) == (True, button)


def test_probe_preserves_inaccessible_challenge():
    page = SimpleNamespace(frames=[_frame(widget=True)])
    assert inspect_press_hold(page, "") == (True, None)


def test_probe_falls_back_to_visible_vendor_host():
    host = Mock()
    page = SimpleNamespace(frames=[_frame(widget=True, hosts=[host])])
    assert inspect_press_hold(page, "") == (True, host)


def test_probe_prefers_child_button_over_parent_host():
    host = Mock()
    button = Mock(is_visible=Mock(return_value=True))
    parent = _frame(widget=True, hosts=[host])
    child = _frame(buttons=[button])
    child.parent_frame = parent
    page = SimpleNamespace(frames=[parent, child])
    assert inspect_press_hold(page, "") == (True, button)


def test_probe_ignores_button_outside_the_challenge_frame():
    unrelated = Mock(is_visible=Mock(return_value=True))
    button = Mock(is_visible=Mock(return_value=True))
    page = SimpleNamespace(
        frames=[
            _frame(buttons=[unrelated]),
            _frame(prompt=True, buttons=[button]),
        ]
    )
    assert inspect_press_hold(page, CHALLENGE) == (True, button)


def test_probe_ignores_hidden_markup_and_ordinary_hold_button():
    button = Mock(is_visible=Mock(return_value=True))
    page = SimpleNamespace(frames=[_frame(buttons=[button])])
    assert inspect_press_hold(page, CHALLENGE) == (False, None)


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr("time.monotonic", lambda: clock.now)
    monkeypatch.setattr(engine, "_behavioral_nudge", lambda page: None)
    return clock


@pytest.mark.parametrize("remaining, expected_ms", [(20, 8000), (2, 2000)])
def test_hold_respects_deadline_and_releases(clock, remaining, expected_ms):
    page, button = Mock(), Mock()
    hold_button(page, button, deadline=remaining)
    button.hover.assert_called_once_with(timeout=1000)
    page.mouse.down.assert_called_once_with(button="left")
    page.wait_for_timeout.assert_called_once_with(expected_ms)
    page.mouse.up.assert_called_once_with(button="left")


@pytest.mark.parametrize("failure", [RuntimeError("detached"), KeyboardInterrupt()])
def test_hold_releases_input_on_failure(clock, failure):
    page = Mock()
    page.wait_for_timeout.side_effect = failure
    with pytest.raises(type(failure)):
        hold_button(page, Mock(), deadline=20)
    page.mouse.up.assert_called_once_with(button="left")


def test_expired_deadline_does_not_press(clock):
    page, button = Mock(), Mock()
    hold_button(page, button, deadline=0)
    button.hover.assert_not_called()
    page.mouse.down.assert_not_called()


class _HoldPage(_FakePage):
    def __init__(self, clock, *, status=200, clears=True, accessible=True):
        super().__init__(status, URL, content=CHALLENGE)
        self.clock = clock
        self.clears = clears
        self.button = Mock(is_visible=Mock(return_value=True))
        self.frames = [_frame(prompt=True, buttons=[self.button] if accessible else [])]
        self.mouse = Mock()
        self.mouse.up.side_effect = self._release

    def wait_for_timeout(self, ms):
        self.clock.now += ms / 1000

    def _release(self, **kwargs):
        if self.clears:
            self._content = CLEARED
            self.frames = [_frame()]
            for callback in self._listeners.get("response", []):
                callback(_FakeResponse(200, URL))


@pytest.mark.parametrize("status", [200, 403])
def test_clear_challenge_holds_then_verifies_redirect(clock, status):
    page = _HoldPage(clock, status=status)
    session = _FakeSession(page, _FakeContext([[]]))
    assert engine.clear_challenge(URL, session=session, settle=0.5) is session
    assert page.mouse.down.call_count == page.mouse.up.call_count == 1
    assert clock.now >= 8.5
    assert not session.closed


@pytest.mark.parametrize("cookies", [[], [{"name": "_px3", "value": "stale"}]])
def test_challenge_never_clears_on_http_200_or_stale_cookie(clock, cookies):
    page = _HoldPage(clock, clears=False)
    session = _FakeSession(page, _FakeContext([cookies]))
    with pytest.raises(engine.WaapChallengeTimeout, match=r"after 2 attempt\(s\)"):
        engine.clear_challenge(URL, session=session, timeout=20, settle=0)
    assert page.mouse.down.call_count == page.mouse.up.call_count == 2
    assert clock.now == 20
    assert not session.closed


@pytest.mark.parametrize("enabled, accessible", [(False, True), (True, False)])
def test_disabled_or_inaccessible_button_waits_without_input(
    clock, enabled, accessible
):
    page = _HoldPage(clock, clears=False, accessible=accessible)
    session = _FakeSession(page, _FakeContext([[{"name": "_px3", "value": "stale"}]]))
    with pytest.raises(engine.WaapChallengeTimeout, match=r"after 0 attempt\(s\)"):
        engine.clear_challenge(URL, session=session, timeout=1, press_hold=enabled)
    page.mouse.down.assert_not_called()


def test_child_frame_response_cannot_clear_top_level_challenge(clock):
    page = _HoldPage(clock, status=403)
    page.main_frame = object()
    original_release = page._release

    def release(**kwargs):
        original_release(**kwargs)
        response = _FakeResponse(403, URL)
        response.request = SimpleNamespace(
            is_navigation_request=lambda: True, frame=object()
        )
        for callback in page._listeners["response"]:
            callback(response)

    page.mouse.up.side_effect = release
    session = _FakeSession(page, _FakeContext([[]]))
    assert engine.clear_challenge(URL, session=session, timeout=10, settle=0) is session


def test_transient_empty_page_with_stale_cookie_is_not_clearance(clock):
    page = _HoldPage(clock, clears=False)

    def release(**kwargs):
        page._content = ""
        page.frames = [_frame()]

    page.mouse.up.side_effect = release
    session = _FakeSession(page, _FakeContext([[{"name": "_px3", "value": "stale"}]]))
    with pytest.raises(engine.WaapChallengeTimeout):
        engine.clear_challenge(URL, session=session, timeout=10, settle=0)


@pytest.mark.parametrize("cookie", ["_px3", "cf_clearance"])
def test_stale_cookie_cannot_skip_late_challenge(clock, cookie):
    page = _HoldPage(clock, clears=False)
    challenge_frames = page.frames
    page.frames = [_frame()]
    page._content = CLEARED
    wait = page.wait_for_timeout

    def show_challenge(ms):
        wait(ms)
        page.frames = challenge_frames
        page._content = CHALLENGE

    page.wait_for_timeout = show_challenge
    session = _FakeSession(page, _FakeContext([[{"name": cookie, "value": "stale"}]]))
    with pytest.raises(engine.WaapChallengeTimeout):
        engine.clear_challenge(URL, session=session, timeout=20, settle=0.5)
    assert page.mouse.down.call_count == 2


@pytest.mark.parametrize("cookie", ["_px3", "cf_clearance"])
@pytest.mark.parametrize("status", [200, 403])
def test_widget_disappearance_requires_a_new_success_response(clock, cookie, status):
    page = _HoldPage(clock, status=status, clears=False)

    def release(**kwargs):
        page.frames = [_frame()]
        page._content = CLEARED

    page.mouse.up.side_effect = release
    session = _FakeSession(page, _FakeContext([[{"name": cookie, "value": "stale"}]]))
    with pytest.raises(engine.WaapChallengeTimeout):
        engine.clear_challenge(URL, session=session, timeout=10, settle=0)


@pytest.mark.parametrize("status", [200, 403])
def test_hidden_challenge_template_does_not_hide_hard_block(clock, status):
    page = _HoldPage(clock, status=status)
    page.frames = [_frame()]
    page._content = (
        "<h1>Access to this page has been denied</h1><div hidden>"
        + CHALLENGE
        + "</div>"
    )
    session = _FakeSession(page, _FakeContext([[]]))
    with pytest.raises(engine.WaapHardBlockError, match="access denied"):
        engine.clear_challenge(URL, session=session, timeout=1, settle=0)
    page.mouse.down.assert_not_called()


def test_non_human_cookie_still_clears_short_body_after_settle(clock):
    page = _HoldPage(clock, status=247)
    page.frames = [_frame()]
    page._content = "OK"
    session = _FakeSession(page, _FakeContext([[{"name": "waap_id", "value": "valid"}]]))
    assert engine.clear_challenge(URL, session=session, timeout=1, settle=0.5) is session
    assert clock.now == 0.5
    page.mouse.down.assert_not_called()
