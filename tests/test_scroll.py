"""Offline tests for wheel-based scrolling.

A human scroll emits a stream of ``wheel`` events; ``window.scrollBy`` fires
only ``scroll``. Behavioral sensors listen for the former, so ``scroll`` must
use real wheel events and still be deterministic when a wheel is swallowed.
No browser starts here: a fake page records the input.
"""

from __future__ import annotations

from wraith.agent import AgentBrowser


class _Mouse:
    def __init__(self, page):
        self.page = page

    def wheel(self, dx, dy):
        self.page.wheel_calls.append((dx, dy))
        if self.page.respond:
            self.page.scroll[0] += dx
            self.page.scroll[1] += dy


class _FakePage:
    def __init__(self, respond=True):
        self.scroll = [0.0, 0.0]
        self.wheel_calls: list[tuple[float, float]] = []
        self.js: list[str] = []
        self.respond = respond
        self.mouse = _Mouse(self)

    def evaluate(self, script):
        if "scrollX" in script:
            return list(self.scroll)
        self.js.append(script)
        return None

    def wait_for_timeout(self, ms):
        return None

    def is_closed(self):
        return False


def _browser(page):
    ab = AgentBrowser()
    ab._active_page = page
    return ab


def test_wheel_scroll_emits_several_events_and_moves_the_page():
    page = _FakePage()
    _browser(page)._wheel_scroll(0, 700)

    assert len(page.wheel_calls) >= 2
    assert abs(sum(dy for _, dy in page.wheel_calls) - 700) < 1e-6
    assert page.scroll == [0.0, 700.0]
    assert page.js == []  # the wheel moved the page; no JS fallback


def test_a_blocked_wheel_falls_back_to_scrollby():
    page = _FakePage(respond=False)
    _browser(page)._wheel_scroll(0, 700)

    assert page.wheel_calls  # it did try the wheel
    assert any(script.startswith("window.scrollBy") for script in page.js)


def test_left_and_right_use_horizontal_wheel_deltas():
    page = _FakePage()
    _browser(page)._wheel_scroll(-300, 0)

    assert page.scroll == [-300.0, 0.0]
    assert all(dy == 0 for _, dy in page.wheel_calls)


def test_scroll_top_uses_scroll_to(monkeypatch):
    page = _FakePage()
    ab = _browser(page)
    ab.snapshot = lambda: "snap"

    assert ab.scroll("top") == "snap"
    assert page.js == ["window.scrollTo(0, 0)"]


def test_scroll_down_uses_the_wheel_path(monkeypatch):
    page = _FakePage()
    ab = _browser(page)
    ab.snapshot = lambda: "snap"

    assert ab.scroll("down", amount=500) == "snap"
    assert page.wheel_calls
    assert page.scroll == [0.0, 500.0]
