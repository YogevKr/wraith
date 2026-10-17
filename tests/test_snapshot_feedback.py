"""Snapshots show what controls hold, and actions report what changed."""

from __future__ import annotations

import wraith.agent as ag
from wraith.agent import AgentBrowser
from wraith.snapshot import Element, Snapshot


def test_a_select_shows_its_choice_and_options():
    select = Element(
        0, "select", "combobox", "Option 2", {"options": "Pick | Option 1 | Option 2"}
    )
    assert select.to_text() == (
        '[0]<select role=combobox options="Pick | Option 1 | Option 2">Option 2</select>'
    )
    other = Element(0, "select", "combobox", "Option 1", dict(select.attributes))
    assert select.signature == other.signature  # same list, another choice


def test_range_and_file_inputs_show_their_state_but_text_fields_hide_values():
    assert (
        'value="5"'
        in Element(1, "input", "slider", "", {"type": "range", "value": "5"}).to_text()
    )
    upload = Element(2, "input", "textbox", "", {"type": "file", "files": "cv.txt"})
    assert 'files="cv.txt"' in upload.to_text()
    secret = Element(
        3, "input", "textbox", "", {"type": "password", "value": "hunter2"}
    )
    assert "hunter2" not in secret.to_text() and "hunter2" not in secret.signature


def test_snapshot_reports_captcha_tokens_without_their_value():
    snap = Snapshot("u", "t", [], challenges={"turnstile": True, "recaptcha": False})
    assert snap.to_text().endswith(
        "Challenges: recaptcha not solved yet, turnstile solved"
    )


def _browser_with_signatures(pre, post):
    page = type(
        "P", (), {"url": "https://a.test/", "evaluate": lambda self, js: post}
    )()
    ab = AgentBrowser(session=type("S", (), {"page": page, "context": None})())
    snap = Snapshot("https://a.test/", "", [])
    ab._set_changed(pre, snap)
    return snap.changed


def test_changed_names_new_text_and_control_values():
    pre = {
        "url": "u",
        "n": 5,
        "h": 1,
        "lines": ["Upload", "Choose a file"],
        "controls": {"country": "Pick", "doc": ""},
    }
    post = {
        "url": "u",
        "n": 5,
        "h": 2,
        "lines": ["Upload", "File Uploaded!", "cv.txt"],
        "controls": {"country": "Israel", "doc": "cv.txt"},
    }
    assert _browser_with_signatures(pre, post) == (
        'country -> Israel; doc -> cv.txt; new text: "File Uploaded!" / "cv.txt"'
    )


def test_changed_keeps_the_old_summary_when_nothing_new_shows():
    pre = {"url": "u", "n": 5, "h": 1, "lines": ["A", "B"], "controls": {}}
    assert _browser_with_signatures(pre, dict(pre)) == "no visible change detected"
    assert (
        _browser_with_signatures(pre, dict(pre, lines=["B"], h=3)) == "content changed"
    )
    assert ag._new_lines(["a"], ["a", "b", "c", "d", "e"]) == ['"b"', '"c"', '"d"']
