"""Offline regression tests for ``AgentBrowser.type()``'s verified-clear path.

Bug this covers: ``locator.fill("")`` silently no-ops against editors whose
real document model is decoupled from the DOM node's raw value — CodeMirror's
classic "hidden textarea" input-capture pattern is the concrete case this was
found against (live, against a real CodeMirror-backed JSON policy editor). No
exception was raised, so the old ``clear=True`` path assumed success and typed
the new text on top of the untouched original document: a ~76-line document
nearly doubled to ~151 lines, with the new text prepended and the original
content glued on right after it — instead of being replaced.

These tests are fully offline (duck-typed fake locators, no browser/network),
matching the existing ``tests/test_agent_ergonomics.py`` fake-session pattern.
"""

from __future__ import annotations

import pytest

import wraith.agent as ag
import wraith.behavior as behavior_mod
from wraith.agent import AgentBrowser, ClearFailedError
from wraith.snapshot import Snapshot


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakePage:
    def __init__(self, locator):
        self._locator = locator

    def locator(self, sel):
        return self._locator

    def evaluate(self, *a, **k):
        return {}


class _FakeSession:
    def __init__(self, page):
        self.page = page


def _agent(locator) -> AgentBrowser:
    return AgentBrowser(session=_FakeSession(_FakePage(locator)))


class _CodeMirrorLikeLocator:
    """A hidden-textarea-backed locator whose real content is JS-owned.

    Mirrors the reported behavior: ``fill("")`` sets nothing (the editor's own
    model wins, the same silent no-op observed live), a genuine keyboard
    select-all+delete DOES clear it, and ``click()`` fails the way a real
    overlay-intercepted pointer click does (the reported ``click(index)``
    timeout) — so a correct fix must not rely on ``click()`` to drive the
    clear fallback.
    """

    def __init__(self, initial: str):
        self.value = initial
        self._selected_all = False
        self.calls: list[str] = []

    def count(self) -> int:
        return 1

    def fill(self, text: str) -> None:
        self.calls.append(f"fill({text!r})")
        if text == "":
            return  # silent no-op, exactly like the observed CodeMirror bug
        # Only reachable when the field was never actually cleared first —
        # model it as a naive prepend so a regression shows up loudly in the
        # resulting value (this is what produced the 76 -> 151 line bug).
        self.value = text + self.value

    def input_value(self) -> str:
        return self.value

    def inner_text(self) -> str:
        return self.value

    def click(self) -> None:
        self.calls.append("click()")
        raise TimeoutError('<div class="CodeMirror-lines"> intercepts pointer events')

    def focus(self) -> None:
        self.calls.append("focus()")

    def press(self, key: str) -> None:
        self.calls.append(f"press({key!r})")
        if key == "ControlOrMeta+a":
            self._selected_all = True
        elif key in ("Backspace", "Delete"):
            if self._selected_all:
                self.value = ""
            self._selected_all = False

    def press_sequentially(self, ch: str, delay: float = 0) -> None:
        self.calls.append(f"type:{ch!r}")
        self.value += ch


class _NeverClearsLocator(_CodeMirrorLikeLocator):
    """Nothing clears it: fill("") no-ops AND the keyboard fallback fails too."""

    def press(self, key: str) -> None:
        self.calls.append(f"press({key!r})")
        # deliberately does not honor select-all+delete


class _PlainInputLocator:
    """An ordinary <input>/<textarea>: fill("") clears it immediately."""

    def __init__(self, initial: str):
        self.value = initial
        self.calls: list[str] = []

    def count(self) -> int:
        return 1

    def fill(self, text: str) -> None:
        self.calls.append(f"fill({text!r})")
        self.value = text

    def input_value(self) -> str:
        return self.value

    def click(self) -> None:
        self.calls.append("click()")

    def focus(self) -> None:
        self.calls.append("focus()")

    def press(self, key: str) -> None:
        self.calls.append(f"press({key!r})")

    def press_sequentially(self, ch: str, delay: float = 0) -> None:
        self.calls.append(f"type:{ch!r}")
        self.value += ch


@pytest.fixture(autouse=True)
def _no_real_snapshot(monkeypatch):
    """type() re-snapshots after acting -- stub it so tests stay offline."""
    monkeypatch.setattr(ag, "take_snapshot", lambda page, **kw: Snapshot("u", "t", []))


@pytest.fixture(autouse=True)
def _fast_human_type(monkeypatch):
    """Skip human_type's real per-key sleeps: deterministic and instant."""

    def _fake(locator, text, **kw):
        locator.click()
        for ch in text:
            locator.press_sequentially(ch, delay=0)

    monkeypatch.setattr(behavior_mod, "human_type", _fake)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_clear_falls_back_to_keyboard_when_fill_is_a_silent_noop():
    """fill("") no-ops on a CodeMirror-like field -> the keyboard fallback
    (focus + ControlOrMeta+a + Backspace/Delete) actually clears it, and the
    new text replaces the old document instead of doubling on top of it."""
    original = (
        '// Example/default ACLs for unrestricted connections.\n{\n  "old": true\n}\n'
    )
    loc = _CodeMirrorLikeLocator(original)
    a = _agent(loc)

    a.type(0, "{}")

    assert loc.value == "{}"
    assert original not in loc.value  # no doubling / prepend of the old doc

    # The clear itself must never rely on click() (it times out on the real
    # overlay) -- only focus()/press() should appear before the keyboard
    # clear lands.
    clear_idx = loc.calls.index("press('ControlOrMeta+a')")
    assert "click()" not in loc.calls[:clear_idx]
    assert "focus()" in loc.calls[:clear_idx]


def test_clear_raises_when_field_cannot_be_verified_empty():
    """If neither fill("") nor the keyboard fallback actually empties the
    field, type() must raise rather than silently typing on unverified
    content -- the exact failure mode that caused the live data-loss bug."""
    loc = _NeverClearsLocator("stuff that never goes away")
    a = _agent(loc)

    with pytest.raises(ClearFailedError):
        a.type(0, "new text")

    assert "new text" not in loc.value
    assert loc.value == "stuff that never goes away"


def test_clear_fast_path_still_used_for_plain_fields():
    """Ordinary fields where fill("") genuinely works should not need (or
    trigger) the keyboard fallback."""
    loc = _PlainInputLocator("old value")
    a = _agent(loc)

    a.type(0, "new value")

    assert loc.value == "new value"
    assert not any(c.startswith("press(") for c in loc.calls)


def test_clear_false_skips_verification_entirely():
    """clear=False must not trigger any clear/verification machinery."""
    loc = _PlainInputLocator("prefix-")
    a = _agent(loc)

    a.type(0, "suffix", clear=False)

    assert loc.value == "prefix-suffix"
    assert not any(c.startswith("fill(") for c in loc.calls)


def test_clear_failed_error_reexported():
    import wraith

    assert hasattr(wraith, "ClearFailedError")
    assert wraith.ClearFailedError is ClearFailedError
