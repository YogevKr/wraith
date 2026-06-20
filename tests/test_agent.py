"""Offline tests for the agent perception/action layer.

These run without a browser binary or network. They assert that:

* the snapshot data model (``Element`` / ``Snapshot``) renders and indexes
  correctly via constructed objects (no live DOM),
* ``Snapshot.to_text`` produces browser-use-style output and ``by_index``
  resolves elements,
* ``AgentBrowser`` and the ``agent_browser`` contextmanager import and carry
  the agreed index-action API surface, and
* the new symbols are re-exported on the top-level ``wraith`` package.
"""

from __future__ import annotations

import inspect

import pytest

from wraith.snapshot import Element, Snapshot, take_snapshot


# --------------------------------------------------------------------------- #
# Snapshot / Element data model (offline, constructed objects)
# --------------------------------------------------------------------------- #

def _sample_snapshot() -> Snapshot:
    return Snapshot(
        url="https://example.com/search",
        title="Example Search",
        elements=[
            Element(
                index=0,
                tag="input",
                role="textbox",
                text="",
                attributes={"type": "text", "placeholder": "Search", "name": "q"},
            ),
            Element(
                index=1,
                tag="button",
                role="button",
                text="Search",
                attributes={"type": "submit"},
            ),
            Element(
                index=2,
                tag="a",
                role="link",
                text="About",
                attributes={"href": "/about"},
            ),
        ],
    )


def test_element_to_text_with_body():
    el = Element(1, "button", "button", "Search", {"type": "submit"})
    line = el.to_text()
    assert line.startswith("[1]<button")
    assert "role=button" in line
    assert ">Search</button>" in line


def test_element_to_text_self_closing_when_no_text():
    el = Element(0, "input", "textbox", "", {"placeholder": "Search"})
    line = el.to_text()
    assert line.startswith("[0]<input")
    assert line.endswith("/>")
    assert 'placeholder="Search"' in line


def test_snapshot_to_text_has_header_and_one_line_per_element():
    snap = _sample_snapshot()
    text = snap.to_text()
    assert "URL: https://example.com/search" in text
    assert "Title: Example Search" in text
    # one indexed line per interactive element
    assert "[0]<input" in text
    assert "[1]<button" in text
    assert "[2]<a" in text
    # the index appears exactly once per element
    for i in (0, 1, 2):
        assert text.count(f"[{i}]<") == 1


def test_snapshot_to_text_empty():
    snap = Snapshot(url="https://x.test/", title="X", elements=[])
    text = snap.to_text()
    assert "URL: https://x.test/" in text
    assert "no interactive elements" in text.lower()


def test_snapshot_by_index():
    snap = _sample_snapshot()
    el = snap.by_index(1)
    assert el is not None
    assert el.tag == "button"
    assert el.text == "Search"
    assert snap.by_index(99) is None


def test_snapshot_len_and_iter():
    snap = _sample_snapshot()
    assert len(snap) == 3
    assert [e.index for e in snap] == [0, 1, 2]


# --------------------------------------------------------------------------- #
# AgentBrowser API surface (import only — no browser launched)
# --------------------------------------------------------------------------- #

def test_take_snapshot_signature():
    sig = inspect.signature(take_snapshot)
    params = sig.parameters
    assert "page" in params
    assert params["viewport_only"].default is True
    assert params["highlight"].default is False
    assert params["max_elements"].default == 200


def test_agent_browser_importable_and_methods_exist():
    from wraith.agent import AgentBrowser, agent_browser

    assert inspect.isclass(AgentBrowser)
    # the agreed perceive/act-by-index API
    for name in (
        "navigate",
        "snapshot",
        "click",
        "type",
        "scroll",
        "read",
        "get_text",
        "screenshot",
        "close",
    ):
        assert callable(getattr(AgentBrowser, name)), f"AgentBrowser.{name} missing"
    # properties
    for prop in ("current_url", "current_title"):
        assert isinstance(getattr(AgentBrowser, prop), property), f"{prop} not a property"
    # context manager protocol
    assert hasattr(AgentBrowser, "__enter__")
    assert hasattr(AgentBrowser, "__exit__")
    # module-level contextmanager factory
    assert callable(agent_browser)


def test_agent_browser_index_action_signatures():
    from wraith.agent import AgentBrowser

    click_sig = inspect.signature(AgentBrowser.click)
    assert "index" in click_sig.parameters

    type_sig = inspect.signature(AgentBrowser.type)
    assert "index" in type_sig.parameters
    assert "text" in type_sig.parameters
    # agreed keyword-only knobs
    assert type_sig.parameters["clear"].default is True
    assert type_sig.parameters["enter"].default is False


def test_agent_browser_borrows_session_without_owning():
    """Passing a session should mark it borrowed (not owned) — pure-Python path."""
    from wraith.agent import AgentBrowser

    sentinel = object()
    ab = AgentBrowser(session=sentinel)
    # internal ownership flag: borrowed sessions are not owned
    assert ab._session is sentinel
    assert ab._owns_session is False


# --------------------------------------------------------------------------- #
# Top-level re-exports
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "name",
    ["AgentBrowser", "agent_browser", "take_snapshot", "Snapshot", "Element"],
)
def test_reexported_on_package(name):
    import wraith

    assert hasattr(wraith, name), f"wraith.{name} missing"
    assert name in wraith.__all__, f"{name} not in wraith.__all__"
