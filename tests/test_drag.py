"""Offline tests for the human drag (slider challenges)."""

from __future__ import annotations

import asyncio

import pytest

import wraith.agent as ag
import wraith.mcp as m
from tests._fakes import NodeHandle
from wraith import behavior
from wraith.agent import AgentBrowser
from wraith.snapshot import Element, Snapshot


@pytest.mark.parametrize("distance", [30, 120, 300])
def test_drag_path_lands_exactly_on_the_target(distance):
    points = behavior.drag_path((100.0, 200.0), (100.0 + distance, 200.0))
    xs = [x for x, _, _ in points]
    assert points[-1][:2] == (100.0 + distance, 200.0)
    assert 18 <= len(points) <= 91
    assert all(wait > 0 for _, _, wait in points)
    if distance > 60:
        assert max(xs) > 100 + distance  # a hand overshoots, then settles
    else:
        assert max(xs) <= 100 + distance + 1.5


def test_drag_path_wobbles_and_takes_human_time():
    points = behavior.drag_path((0.0, 0.0), (250.0, 0.0))
    ys = [y for _, y, _ in points[:-1]]
    total_ms = sum(wait for _, _, wait in points)
    assert max(abs(y) for y in ys) > 0.3  # never a ruler-straight line
    assert 450 < total_ms < 2000


class _Mouse:
    def __init__(self, fail_at=None):
        self.events, self.fail_at = [], fail_at

    def down(self):
        self.events.append("down")

    def move(self, x, y):
        self.events.append(("move", round(x), round(y)))
        if self.fail_at is not None and len(self.events) == self.fail_at:
            raise RuntimeError("page closed")

    def up(self):
        self.events.append("up")


class _Page:
    url = "https://slider.test/"

    def __init__(self, mouse=None, node=None):
        self.mouse = mouse or _Mouse()
        self.node = node
        self.waits = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def evaluate_handle(self, js, index):
        return NodeHandle(self.node)


@pytest.fixture(autouse=True)
def _no_approach(monkeypatch):
    monkeypatch.setattr(behavior, "human_move", lambda page, target=None, **kw: None)


def test_human_drag_holds_moves_and_releases():
    page = _Page()
    behavior.human_drag(page, (10.0, 20.0), (210.0, 20.0))
    events = page.mouse.events
    assert events[0] == "down" and events[-1] == "up"
    assert events[-2] == ("move", 210, 20)
    assert page.waits[1] >= 60  # held before moving


def test_human_drag_always_releases_the_button():
    page = _Page(_Mouse(fail_at=5))
    with pytest.raises(RuntimeError, match="page closed"):
        behavior.human_drag(page, (0.0, 0.0), (100.0, 0.0))
    assert page.mouse.events[-1] == "up"


@pytest.fixture
def browser(monkeypatch):
    def make(node=None):
        page = _Page(node=node)
        ab = AgentBrowser(session=type("S", (), {"page": page, "context": None})())
        monkeypatch.setattr(
            ag, "_await_navigation", lambda p, action, allows=None: action()
        )
        monkeypatch.setattr(AgentBrowser, "_wait_for_settle", lambda self: None)
        monkeypatch.setattr(
            AgentBrowser, "snapshot", lambda self: Snapshot(page.url, "", [])
        )
        drags = []
        monkeypatch.setattr(
            behavior, "human_drag", lambda p, start, end: drags.append((start, end))
        )
        return ab, drags

    return make


def test_drag_from_a_page_point(browser):
    ab, drags = browser()
    ab.drag(150, -10, x=300, y=400)
    assert drags == [((300.0, 400.0), (450.0, 390.0))]
    with pytest.raises(ValueError, match="index or a page point"):
        ab.drag(10)
    with pytest.raises(ValueError, match="index or a page point"):
        ab.drag(10, x=-5, y=3)


def test_drag_from_an_indexed_knob(browser):
    class _Knob:
        def scroll_into_view_if_needed(self):
            pass

        def evaluate(self, js):
            assert js == ag._POINTER_POINT_JS
            return {"x": 40.0, "y": 60.0}

    ab, drags = browser(_Knob())
    ab.last_snapshot = Snapshot("u", "", [Element(3, "div", "slider", "", {})])
    ab.drag(200, index=3)
    assert drags == [((40.0, 60.0), (240.0, 60.0))]


def test_mcp_drag_passes_its_arguments(monkeypatch):
    calls = []

    class FakeBrowser:
        def drag(self, dx, dy, **kw):
            calls.append((dx, dy, kw))
            return Snapshot("https://slider.test/", "", [])

    monkeypatch.setattr(m, "_get_browser", lambda: FakeBrowser())
    asyncio.run(m.drag(212, x=1170, y=737))
    asyncio.run(m.drag(80, 5, index=2))
    assert calls == [
        (212, 0.0, {"index": None, "x": 1170, "y": 737}),
        (80, 5, {"index": 2, "x": None, "y": None}),
    ]
