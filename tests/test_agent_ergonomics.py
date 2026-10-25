"""Offline tests for agent ergonomics: signatures, new-marking, change-observation, self-heal."""
from __future__ import annotations

import wraith.agent as ag
from wraith.agent import AgentBrowser
from wraith.snapshot import Element, Snapshot
from tests._fakes import NodeHandle


# --- snapshot.Element / Snapshot ------------------------------------------- #

def test_element_signature_stable_and_distinct():
    a = Element(0, "button", "button", "Search", {"id": "go"})
    b = Element(5, "button", "button", "Search", {"id": "go"})  # diff index, same content
    c = Element(0, "button", "button", "Cancel", {"id": "x"})
    assert a.signature == b.signature
    assert a.signature != c.signature


def test_element_new_marker_in_text():
    e = Element(3, "a", "link", "Home", {"href": "/"})
    e.is_new = True
    assert e.to_text().startswith("* [3]")
    e.is_new = False
    assert e.to_text().startswith("[3]")


def test_snapshot_changed_in_text():
    s = Snapshot("http://x", "T", [], changed="url changed -> http://y")
    assert "Changed: url changed -> http://y" in s.to_text()


# --- AgentBrowser (fake session, no browser launch) ------------------------ #

class _FakePage:
    def __init__(self, sig=None):
        self._sig = sig or {}

    def evaluate(self, *a, **k):
        return self._sig


class _FakeSession:
    def __init__(self, page):
        self.page = page


def _agent(page):
    return AgentBrowser(session=_FakeSession(page))


def test_set_changed_url_and_elements():
    a = _agent(_FakePage({"url": "http://b", "n": 12, "h": 5}))
    snap = Snapshot("http://b", "T", [])
    a._set_changed({"url": "http://a", "n": 10, "h": 1}, snap)
    assert "url changed" in snap.changed
    assert "+2 elements" in snap.changed


def test_set_changed_no_visible_change():
    a = _agent(_FakePage({"url": "http://a", "n": 10, "h": 1}))
    snap = Snapshot("http://a", "T", [])
    a._set_changed({"url": "http://a", "n": 10, "h": 1}, snap)
    assert snap.changed == "no visible change detected"


def test_snapshot_marks_new_elements(monkeypatch):
    a = _agent(_FakePage())
    snaps = iter([
        Snapshot("u", "t", [Element(0, "button", "button", "A", {"id": "a"})]),
        Snapshot("u", "t", [
            Element(0, "button", "button", "A", {"id": "a"}),
            Element(1, "button", "button", "B", {"id": "b"}),
        ]),
    ])
    monkeypatch.setattr(ag, "take_snapshot", lambda page, **kw: next(snaps))
    s1 = a.snapshot()
    assert all(not e.is_new for e in s1.elements)  # no prior -> nothing "new"
    s2 = a.snapshot()
    new = [e for e in s2.elements if e.is_new]
    assert len(new) == 1 and new[0].text == "B"


def test_locator_self_heals_by_signature(monkeypatch):
    class FakePage2:
        def __init__(self):
            self.requested = []

        def evaluate_handle(self, js, index):
            self.requested.append(index)
            return NodeHandle(None if index == 7 else object())  # 7 is stale

        def evaluate(self, *a, **k):
            return {}

    page = FakePage2()
    a = _agent(page)
    a.last_snapshot = Snapshot("u", "t", [Element(7, "button", "button", "Go", {"id": "go"})])
    fresh = Snapshot("u", "t", [Element(2, "button", "button", "Go", {"id": "go"})])
    monkeypatch.setattr(ag, "take_snapshot", lambda page, **kw: fresh)
    a._locator(7)
    assert page.requested[-1] == 2  # healed to the re-resolved index


# --- double-click and human locator click ---------------------------------- #

def test_dblclick_sends_a_double_click(monkeypatch):
    a = AgentBrowser(session=object())
    seen: dict = {}
    monkeypatch.setattr(a, "_pointer_click", lambda index, count=1: seen.update(i=index, c=count))
    monkeypatch.setattr(a, "_act", lambda action: action() or "snap")

    assert a.dblclick(7) == "snap"
    assert seen == {"i": 7, "c": 2}


def test_click_sends_a_single_click(monkeypatch):
    a = AgentBrowser(session=object())
    seen: dict = {}
    monkeypatch.setattr(a, "_pointer_click", lambda index, count=1: seen.update(i=index, c=count))
    monkeypatch.setattr(a, "_act", lambda action: action() or "snap")

    a.click(3)
    assert seen == {"i": 3, "c": 1}


class _LocMouse:
    def __init__(self):
        self.moves = 0

    def move(self, x, y, steps=1):
        self.moves += 1


class _LocPage:
    def __init__(self):
        self.mouse = _LocMouse()

    def evaluate(self, script):
        return [1000, 800]

    def wait_for_timeout(self, _ms):
        pass


class _Locator:
    def __init__(self):
        self.clicked = 0

    def bounding_box(self):
        return {"x": 100.0, "y": 200.0, "width": 80.0, "height": 40.0}

    def click(self, **_kw):
        self.clicked += 1


def test_human_click_locator_moves_the_cursor_first():
    page, loc = _LocPage(), _Locator()
    ag._human_click_locator(page, loc)
    assert loc.clicked == 1
    assert page.mouse.moves > 1  # a human path, not one jump


def test_human_click_locator_falls_back_without_a_box():
    class _NoBox(_Locator):
        def bounding_box(self):
            return None

    page, loc = _LocPage(), _NoBox()
    ag._human_click_locator(page, loc)
    assert loc.clicked == 1
