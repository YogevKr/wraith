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
from tests._fakes import NodeHandle


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakePage:
    def __init__(self, locator):
        self._locator = locator

    def evaluate_handle(self, js, index):
        return NodeHandle(self._locator)

    def evaluate(self, *a, **k):
        return {}


class _FakeSession:
    def __init__(self, page):
        self.page = page


def _agent(locator) -> AgentBrowser:
    return AgentBrowser(session=_FakeSession(_FakePage(locator)))


class _Keys:
    """``press`` of one character types it, as Playwright's ``press`` does."""

    def press(self, key: str, delay: float = 0) -> None:
        if len(key) == 1:
            self.press_sequentially(key)
        else:
            self._key(key)


class _CodeMirrorLikeLocator(_Keys):
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

    def _key(self, key: str) -> None:
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

    def _key(self, key: str) -> None:
        self.calls.append(f"press({key!r})")
        # deliberately does not honor select-all+delete


class _PlainInputLocator(_Keys):
    """An ordinary <input>/<textarea>: fill("") clears it immediately."""

    def __init__(self, initial: str):
        self.value = initial
        self.calls: list[str] = []

    def fill(self, text: str) -> None:
        self.calls.append(f"fill({text!r})")
        self.value = text

    def input_value(self) -> str:
        return self.value

    def click(self) -> None:
        self.calls.append("click()")

    def focus(self) -> None:
        self.calls.append("focus()")

    def _key(self, key: str) -> None:
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

    def _fake(locator, text, click=True, **kw):
        if click:
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


class _SwapOnFirstKeyField(_Keys):
    """A focused input that the page replaces after the first key.

    Wikipedia's search box does this: the pinned handle detaches, focus moves
    to the new input, and the typed text continues there.
    """

    def __init__(self):
        self.value = ""
        self.attached = True
        self.calls: list[str] = []

    def evaluate(self, js, *a):
        return self.attached  # focus is on this node until it is replaced

    def fill(self, text: str) -> None:
        if not self.attached:
            raise RuntimeError("ElementHandle.fill: Element is not attached to the DOM")
        self.value = text

    def input_value(self) -> str:
        return self.value

    def focus(self) -> None:
        self.calls.append("focus()")

    def press(self, key: str, delay: float = 0) -> None:
        if not self.attached:
            raise RuntimeError("ElementHandle.press: Element is not attached to the DOM")
        super().press(key, delay)

    def _key(self, key: str) -> None:
        self.calls.append(f"press({key!r})")

    def press_sequentially(self, ch: str, delay: float = 0) -> None:
        if not self.attached:
            raise RuntimeError("ElementHandle.type: Element is not attached to the DOM")
        self.value += ch
        self.attached = False


class _FocusedPageKeyboard:
    """page.keyboard: keys reach the focused input, even after a node swap."""

    def __init__(self, value: str = ""):
        self.value = value
        self.pressed: list[str] = []
        self._selected_all = False

    def type(self, text: str, delay: float = 0) -> None:
        self.value += text

    def press(self, key: str, delay: float = 0) -> None:
        if len(key) == 1:  # a typed character
            self.value += key
            return
        self.pressed.append(key)
        if key == "ControlOrMeta+a":
            self._selected_all = True
        elif key == "Backspace" and self._selected_all:
            self.value = ""


class _FocusedPage(_FakePage):
    """A page whose focused control is modeled by its keyboard."""

    def __init__(self, field, value: str = ""):
        super().__init__(field)
        self.keyboard = _FocusedPageKeyboard(value)

    def evaluate(self, js, *a, **k):
        if js == ag._FOCUSED_VALUE_JS:
            return self.keyboard.value
        return {}


def test_type_follows_focus_when_the_field_is_replaced():
    field = _SwapOnFirstKeyField()
    page = _FocusedPage(field)
    a = AgentBrowser(session=_FakeSession(page))

    a.type(2, "Firefox", enter=True)

    assert page.keyboard.value == "Firefox"
    assert page.keyboard.pressed == ["Enter"]  # empty field: no clear keys
    assert field.value == ""  # no key went to the detached handle


def test_focused_clear_uses_keys_and_verifies():
    field = _SwapOnFirstKeyField()
    page = _FocusedPage(field, value="old query")
    a = AgentBrowser(session=_FakeSession(page))

    a.type(2, "Firefox")

    assert page.keyboard.value == "Firefox"
    assert page.keyboard.pressed == ["ControlOrMeta+a", "Backspace"]


def test_focused_clear_refuses_unverified_content():
    class _StickyKeyboard(_FocusedPageKeyboard):
        def press(self, key: str, delay: float = 0) -> None:
            self.pressed.append(key)  # select-all + delete has no effect

    field = _SwapOnFirstKeyField()
    page = _FocusedPage(field)
    page.keyboard = _StickyKeyboard("old query")
    a = AgentBrowser(session=_FakeSession(page))

    with pytest.raises(ClearFailedError):
        a.type(2, "Firefox")

    assert page.keyboard.value == "old query"


class _NavRequest:
    def __init__(self, frame, url="https://example.test/next"):
        self.frame, self.url = frame, url

    def is_navigation_request(self):
        return True


class _NavigatingPage(_FocusedPage):
    """Fires Playwright's request and framenavigated events while it waits.

    A navigation starts on the first wait after the action, as on a slow
    machine, and commits on the next one. ``clock`` is the fake time.
    """

    def __init__(self, field, *, navigates: bool, url="https://example.test/next"):
        super().__init__(field)
        self.navigates, self.url = navigates, url
        self.events: list[str] = []
        self.handlers: dict = {}
        self.main_frame = object()
        self.clock = 0.0
        self._step = 0

    def on(self, event, handler):
        self.handlers[event] = handler

    def remove_listener(self, event, handler):
        self.handlers.pop(event, None)

    def wait_for_timeout(self, ms):
        self.clock += ms / 1000
        if not self.navigates or "request" not in self.handlers:
            return
        self._step += 1
        if self._step == 1:
            self.events.append("request")
            self.handlers["request"](_NavRequest(self.main_frame, self.url))
        elif self._step == 2:
            self.events.append("committed")
            self.handlers["framenavigated"](self.main_frame)


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch, request):
    """time.monotonic follows the newest _NavigatingPage's clock."""
    pages: list = []
    real_init = _NavigatingPage.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        pages.append(self)

    monkeypatch.setattr(_NavigatingPage, "__init__", init)
    real_monotonic = ag.time.monotonic
    monkeypatch.setattr(ag.time, "monotonic", lambda: pages[-1].clock if pages else real_monotonic())


@pytest.mark.parametrize("navigates", [True, False])
def test_enter_waits_only_for_a_navigation_it_starts(navigates):
    page = _NavigatingPage(_SwapOnFirstKeyField(), navigates=navigates)
    a = AgentBrowser(session=_FakeSession(page))

    a.type(2, "Firefox", enter=True)

    assert page.keyboard.pressed == ["Enter"]
    assert ("committed" in page.events) is navigates
    assert page.handlers == {}  # listeners removed
    if not navigates:
        assert page.clock < 1.0  # the start window only, no commit wait


def test_type_refuses_a_non_editable_target_before_clicking():
    class _DonateLink:
        def __init__(self):
            self.calls: list[str] = []

        def evaluate(self, js, *a):
            return False if js == ag._EDITABLE_JS else True

        def element_handle(self):
            self.calls.append("element_handle()")
            return self

        def click(self, *a, **k):
            self.calls.append("click()")

        def focus(self):
            self.calls.append("focus()")

    link = _DonateLink()
    a = AgentBrowser(session=_FakeSession(_FocusedPage(link)))

    with pytest.raises(RuntimeError, match="not a text field"):
        a.type(2, "Firefox", enter=True)

    assert link.calls == []
    assert a.page.keyboard.pressed == []


def test_await_navigation_propagates_action_errors():
    page = _NavigatingPage(_SwapOnFirstKeyField(), navigates=False)

    def _covered():
        raise RuntimeError("The visible target is covered or unavailable")

    with pytest.raises(RuntimeError, match="covered"):
        ag._await_navigation(page, _covered)


def test_click_waits_for_the_navigation_it_starts(monkeypatch):
    page = _NavigatingPage(_SwapOnFirstKeyField(), navigates=True)
    a = AgentBrowser(session=_FakeSession(page))
    monkeypatch.setattr(a, "_pointer_click", lambda index: page.events.append(f"click {index}"))

    a.click(4)

    assert page.events == ["click 4", "request", "committed"]


def test_the_wait_window_starts_when_a_slow_action_ends():
    # Playwright's expect_request timer started before the action, so a click
    # that took longer than the window returned before the new page committed.
    page = _NavigatingPage(_SwapOnFirstKeyField(), navigates=True)

    def slow_click():
        page.clock += 2.0  # a human-like click on a slow machine

    ag._await_navigation(page, slow_click)
    assert page.events == ["request", "committed"]


def test_a_refused_navigation_is_not_awaited():
    page = _NavigatingPage(_SwapOnFirstKeyField(), navigates=True, url="https://evil.test/")
    ag._await_navigation(page, lambda: None, allows=lambda url: "evil" not in url)
    assert page.events[0] == "request"
    assert page.clock <= ag._NAVIGATION_START_MS / 1000 + 0.05  # no commit wait
