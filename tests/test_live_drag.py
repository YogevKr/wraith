"""Opt-in drag check: WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_drag.py.

A local "slide to verify" widget on 127.0.0.1 records every mouse event, the
way slider challenges score a drag.
"""

from __future__ import annotations

import json
import os

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip("set WRAITH_LIVE_TEST=1 to run the drag check", allow_module_level=True)

from tests._local_server import html_pages, serve
from wraith.agent import AgentBrowser
from wraith.engine import launch

PAD = "<p>" + "Fixture text for a page with real content. " * 8 + "</p>"
SLIDER = f"""<!doctype html><title>Slide</title><body>{PAD}
<div id=track style="position:relative;width:320px;height:40px;background:#ddd;margin:40px">
  <div id=knob role=slider tabindex=0 aria-label="Slide to verify"
       style="position:absolute;left:0;top:0;width:40px;height:40px;background:#36c"></div>
</div>
<script>
const knob = document.getElementById('knob');
const log = {{events: [], verified: false}};
let start = null;
const save = () => {{ document.documentElement.dataset.slide = JSON.stringify(log); }};
const note = e => log.events.push({{t: e.type, trusted: e.isTrusted, x: e.clientX, y: e.clientY, at: performance.now()}});
knob.addEventListener('mousedown', e => {{ note(e); start = e.clientX - knob.offsetLeft; save(); }});
document.addEventListener('mousemove', e => {{
  if (start === null) return;
  note(e);
  knob.style.left = Math.max(0, Math.min(280, e.clientX - start)) + 'px';
}});
document.addEventListener('mouseup', e => {{
  if (start === null) return;
  note(e); start = null;
  log.verified = knob.offsetLeft >= 270;
  save();
}});
save();
</script></body>"""


_Handler = html_pages({}, SLIDER)


@pytest.mark.parametrize("engine", ["camoufox", "chromium"])
def test_drag_moves_a_slider_like_a_hand(engine):
    with (
        serve(_Handler) as server,
        launch(engine=engine, headless=True, geoip=False) as session,
    ):
        ab = AgentBrowser(session=session)
        snap = ab.navigate(f"http://127.0.0.1:{server.server_port}/")
        knob = next(el.index for el in snap.elements if el.role == "slider")
        ab.drag(280, index=knob)
        log = json.loads(
            session.page.evaluate("() => document.documentElement.dataset.slide")
        )

    events = log["events"]
    down = next(e for e in events if e["t"] == "mousedown")
    moves = [e for e in events if e["t"] == "mousemove"]
    up = next(e for e in events if e["t"] == "mouseup")
    assert log["verified"], events[-3:]
    assert all(e["trusted"] for e in events)
    assert len(moves) >= 15
    assert moves[0]["at"] - down["at"] >= 40  # held before moving
    assert up["at"] - down["at"] >= 400  # a human-length drag
    assert len({e["y"] for e in moves}) > 1  # not a ruler-straight line
