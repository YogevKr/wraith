"""Offline tests for frame-aware snapshots and actions (fake Playwright frames)."""

from __future__ import annotations

import asyncio
import inspect

import pytest

import wraith.agent as ag
import wraith.mcp as m
from wraith import snapshot as snap_mod
from wraith.agent import AgentBrowser
from wraith.secrets import SecretPolicyError
from tests._fakes import NodeHandle


def _item(tag, text, x, y, w=80, h=20, **attrs):
    return {"tag": tag, "role": "", "text": text, "attributes": attrs,
            "bounds": {"x": x, "y": y, "width": w, "height": h}}


class _IFrame:
    """An <iframe> element handle in its parent document."""

    def __init__(self, box, covered=False):
        self.box = box
        self.covered = covered

    def evaluate(self, js, *a):
        assert js == snap_mod._FRAME_BOX_JS
        return self.box


class _Frame:
    def __init__(self, url, items, *, iframe=None, children=()):
        self.url = url
        self.items = items
        self.iframe = iframe
        self.child_frames = list(children)
        self.walks: list[dict] = []
        self.lookups: list[int] = []

    def frame_element(self):
        return self.iframe

    def evaluate_handle(self, js, index):
        assert js == snap_mod._INDEXED_NODE_JS
        self.lookups.append(index)
        return NodeHandle(_Locator(self, index))

    def evaluate(self, js, arg=None):
        if js == snap_mod._BUILD_DOM_TREE_JS:
            self.walks.append(arg)
            start = arg.get("startIndex", 0)
            return [dict(item, index=start + i) for i, item in enumerate(self.items)]
        if js == snap_mod._HITS_FRAME_JS:
            iframe, points = arg
            return [not iframe.covered for _ in points]
        if "innerWidth" in js:
            return [1000, 800]
        raise AssertionError(js)


class _Locator:
    def __init__(self, frame, index):
        self.frame, self.index = frame, index

    def element_handle(self):
        return self

    def click(self, **kw):
        self.frame.clicked = (self.index, kw)


class _Page:
    def __init__(self, main):
        self.main_frame = main
        self.url = main.url

    def title(self):
        return "Parent"

    def evaluate(self, js, arg=None):
        return self.main_frame.evaluate(js, arg)

    def evaluate_handle(self, js, index):
        return self.main_frame.evaluate_handle(js, index)


def _page():
    nested = _Frame("https://grandchild.test/", [_item("button", "Nested", 5, 5)],
                    iframe=_IFrame({"x": 10, "y": 100, "width": 200, "height": 60}))
    form = _Frame("https://child.test/form", [_item("input", "", 4, 4, name="q"),
                                               _item("button", "Child submit", 4, 40)],
                  iframe=_IFrame({"x": 100, "y": 200, "width": 400, "height": 260}),
                  children=[nested])
    opaque = _Frame("https://child.test/opaque", [],
                    iframe=_IFrame({"x": 100, "y": 500, "width": 300, "height": 80}))
    covered = _Frame("https://child.test/covered", [_item("button", "Hidden", 4, 4)],
                     iframe=_IFrame({"x": 100, "y": 600, "width": 300, "height": 80}, covered=True))
    pixel = _Frame("about:blank", [_item("a", "Pixel", 0, 0)],
                   iframe=_IFrame({"x": 0, "y": 0, "width": 1, "height": 1}))
    main = _Frame("https://parent.test/", [_item("button", "Parent button", 10, 10)],
                  children=[form, opaque, covered, pixel])
    return _Page(main), form, nested


def test_snapshot_indexes_visible_frames_in_one_sequence():
    page, form, nested = _page()

    snap = snap_mod.take_snapshot(page)

    by_text = {el.text: el for el in snap.elements}
    assert [el.index for el in snap.elements] == [0, 1, 2, 3]
    assert by_text["Parent button"].frame == 0
    assert by_text["Child submit"].frame == 1
    assert by_text["Nested"].frame == 2
    assert (by_text["Child submit"].x, by_text["Child submit"].y) == (104, 240)
    assert (by_text["Nested"].x, by_text["Nested"].y) == (115, 305)
    assert "Hidden" not in by_text and "Pixel" not in by_text
    assert form.walks[0]["startIndex"] == 1 and nested.walks[0]["startIndex"] == 3
    assert [(f.number, f.url, f.element_count) for f in snap.frames] == [
        (1, "https://child.test/form", 2),
        (2, "https://grandchild.test/", 1),
        (3, "https://child.test/opaque", 0),
    ]
    text = snap.to_text()
    assert "Child submit</button> (frame 1)" in text
    assert "frame 3 @ (250,540) 300x80 https://child.test/opaque" in text
    assert "click_frame(3, x, y)" in text


def test_snapshot_without_frames_keeps_the_old_shape():
    page, form, _ = _page()

    snap = snap_mod.take_snapshot(page, include_frames=False)

    assert [el.text for el in snap.elements] == ["Parent button"]
    assert snap.frames == [] and form.walks == []


@pytest.fixture
def browser(monkeypatch):
    page, form, nested = _page()
    a = AgentBrowser(session=type("S", (), {"page": page})())
    a.snapshot()
    return a, page, form


def test_frame_elements_resolve_and_click_in_their_frame(browser):
    a, page, form = browser
    submit = next(el.index for el in a.last_snapshot.elements if el.text == "Child submit")

    a._pointer_click(submit)

    index, options = form.clicked
    assert index == submit
    assert options["timeout"] == ag._FRAME_CLICK_TIMEOUT_MS
    assert 45 <= options["delay"] <= 110  # the button is held like a person's
    assert form.lookups and not page.main_frame.lookups


def test_secret_fills_refuse_frame_fields(browser):
    a, _, _ = browser
    query = next(el.index for el in a.last_snapshot.elements if el.attributes.get("name") == "q")
    capability = {"provider": "none", "handle": "opaque",
                  "allowed_origins": ["https://parent.test"], "field_kind": "text"}

    with pytest.raises(SecretPolicyError, match="main-frame"):
        a.fill_secret(query, capability)
    with pytest.raises(SecretPolicyError, match="main-frame"):
        a.fill_vault_item(query, "item", field_kind="text")


def test_click_at_moves_then_clicks_the_point(monkeypatch):
    calls = []

    class _Mouse:
        def click(self, x, y, **_kwargs):
            calls.append(("click", x, y))

    page = type("P", (), {"mouse": _Mouse()})()
    monkeypatch.setattr("wraith.behavior.human_move", lambda p, target=None, **k: calls.append(("move", target)))
    aims = iter([(250.0, 540.0), (252.0, 538.0)])  # the target moved during travel

    ag._human_click(page, lambda: next(aims))

    assert calls == [("move", (250.0, 540.0)), ("click", 252.0, 538.0)]


def test_click_frame_aims_at_a_fraction_of_the_live_frame_box(browser, monkeypatch):
    a, page, _ = browser
    opaque = next(f for f in a.last_snapshot.frames if f.url.endswith("/opaque"))
    boxes = iter([{"x": 100, "y": 500, "width": 300, "height": 80},
                  {"x": 110, "y": 520, "width": 300, "height": 80}])
    opaque.handle.iframe.bounding_box = lambda: next(boxes)
    clicks = []
    monkeypatch.setattr(ag, "_await_navigation", lambda p, action, allows=None: action())
    monkeypatch.setattr(ag, "_human_click", lambda p, aim: (aim(), clicks.append(aim())))
    monkeypatch.setattr(AgentBrowser, "_page_signature", lambda self: None)

    a.click_frame(opaque.number, 0.1, 0.5)

    assert clicks == [(140.0, 560.0)]


def test_click_frame_rejects_unknown_frames_and_bad_fractions(browser):
    a, _, _ = browser
    with pytest.raises(ValueError, match="between 0 and 1"):
        a.click_frame(1, 1.5, 0.5)
    with pytest.raises(ValueError, match="not in the last snapshot"):
        a.click_frame(99, 0.5, 0.5)


def test_click_at_rejects_points_outside_the_viewport(browser):
    a, _, _ = browser
    with pytest.raises(ValueError):
        a.click_at(-1, 10)


def test_mcp_click_at_and_css_screenshots(monkeypatch):
    assert inspect.iscoroutinefunction(m.click_at)
    assert inspect.iscoroutinefunction(m.click_frame)
    scales = []

    class FakeBrowser:
        def screenshot(self, **kw):
            scales.append(kw.get("scale"))
            return b"\x89PNG"

    monkeypatch.setattr(m, "_get_browser", lambda: FakeBrowser())
    asyncio.run(m.screenshot())
    assert scales == ["css"]


def test_click_frame_works_when_only_an_opaque_frame_is_visible(monkeypatch):
    opaque = _Frame("https://challenges.example/turnstile", [],
                    iframe=_IFrame({"x": 100, "y": 500, "width": 300, "height": 80}))
    opaque.iframe.bounding_box = lambda: {"x": 100, "y": 500, "width": 300, "height": 80}
    page = _Page(_Frame("https://parent.test/", [], children=[opaque]))
    a = AgentBrowser(session=type("S", (), {"page": page})())
    snap = a.snapshot()
    assert len(snap) == 0 and [f.number for f in snap.frames] == [1]
    clicks = []
    monkeypatch.setattr(ag, "_await_navigation", lambda p, action, allows=None: action())
    monkeypatch.setattr(ag, "_human_click", lambda p, aim: clicks.append(aim()))
    monkeypatch.setattr(AgentBrowser, "_page_signature", lambda self: None)

    a.click_frame(1, 0.5, 0.5)

    assert clicks == [(250.0, 540.0)]
