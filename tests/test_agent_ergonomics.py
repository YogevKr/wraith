"""Offline tests for agent ergonomics: signatures, new-marking, change-observation, self-heal."""
from __future__ import annotations

import wraith.agent as ag
from wraith.agent import AgentBrowser
from wraith.snapshot import Element, Snapshot


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
    class FakeLoc:
        def __init__(self, n):
            self._n = n

        def count(self):
            return self._n

    class FakePage2:
        def __init__(self):
            self.requested = []

        def locator(self, sel):
            self.requested.append(sel)
            return FakeLoc(0 if 'index="7"' in sel else 1)  # 7 is stale, others resolve

        def evaluate(self, *a, **k):
            return {}

    page = FakePage2()
    a = _agent(page)
    a.last_snapshot = Snapshot("u", "t", [Element(7, "button", "button", "Go", {"id": "go"})])
    fresh = Snapshot("u", "t", [Element(2, "button", "button", "Go", {"id": "go"})])
    monkeypatch.setattr(ag, "take_snapshot", lambda page, **kw: fresh)
    a._locator(7)
    assert 'index="2"' in page.requested[-1]  # healed to the re-resolved index
