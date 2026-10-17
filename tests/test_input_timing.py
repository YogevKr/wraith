"""Clicks and keystrokes hold the button or key down for a human time."""

from __future__ import annotations

import pytest

import wraith.agent as ag
from wraith import behavior


class _Keys:
    def __init__(self, unknown=False, error=None):
        self.unknown, self.error = unknown, error
        self.pressed, self.typed = [], []

    def press(self, key, delay=0):
        if self.error:
            raise self.error
        if self.unknown:
            raise RuntimeError(f'Keyboard.press: Unknown key: "{key}"')
        self.pressed.append((key, delay))

    def press_sequentially(self, text, delay=0):
        self.typed.append(text)


def test_a_character_is_held_between_keydown_and_keyup():
    keys = _Keys()
    behavior._press_char(keys, "a")
    ((key, delay),) = keys.pressed
    assert key == "a" and 35 <= delay <= 95


def test_a_character_without_a_key_is_inserted():
    keys = _Keys(unknown=True)
    behavior._press_char(keys, "é")
    assert keys.typed == ["é"]


def test_other_press_errors_are_not_hidden():
    keys = _Keys(error=RuntimeError("Element is not attached to the DOM"))
    with pytest.raises(RuntimeError, match="not attached"):
        behavior._press_char(keys, "a")
    assert keys.typed == []


def test_human_click_holds_the_button(monkeypatch):
    clicks = []
    page = type(
        "P",
        (),
        {
            "mouse": type(
                "M", (), {"click": lambda self, x, y, **kw: clicks.append(kw)}
            )()
        },
    )()
    monkeypatch.setattr(behavior, "human_move", lambda *a, **k: None)

    ag._human_click(page, lambda: (10.0, 20.0))

    assert 45 <= clicks[0]["delay"] <= 110


def test_human_type_click_holds_the_button(monkeypatch):
    monkeypatch.setattr(behavior.time, "sleep", lambda s: None)
    clicks = []

    class _Locator(_Keys):
        def click(self, **kw):
            clicks.append(kw)

    behavior.human_type(_Locator(), "hi")
    assert 45 <= clicks[0]["delay"] <= 110
