"""Opt-in frame checks: WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_frames.py.

Local fixture pages only. A parent page holds a rotated cross-origin form
frame with a nested frame, an opaque frame (closed shadow root) placed in a
closed shadow root like Cloudflare Turnstile, and a frame under an overlay.
"""

from __future__ import annotations

import os

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip("set WRAITH_LIVE_TEST=1 to run the frame checks", allow_module_level=True)

from wraith.agent import AgentBrowser
from wraith.engine import launch
from wraith.secrets import SecretPolicyError

PAD = "<p>" + "Fixture text for a page with real content. " * 8 + "</p>"
PAGES = {
    "https://parent.test/": f"""<!doctype html><title>Parent</title>
<h1>Parent</h1><button id=top>Parent button</button>{PAD}
<div style="transform: rotate(3deg); width: 420px; margin: 20px">
  <iframe src="https://child.test/form" width=400 height=260></iframe>
</div>
<div id=widget></div>
<script>
  // Like Turnstile: the iframe lives inside a closed shadow root.
  const shadow = widget.attachShadow({{mode: 'closed'}});
  shadow.innerHTML = '<iframe src="https://child.test/opaque" width=300 height=80></iframe>';
</script>
<div style="position: relative; width: 320px; height: 100px">
  <iframe src="https://child.test/covered" width=300 height=80></iframe>
  <div style="position: absolute; inset: 0; background: #ddd">Overlay</div>
</div>""",
    "https://child.test/form": """<!doctype html>
<input name=q placeholder="Child query">
<button id=go onclick="out.textContent = 'submitted:' + document.querySelector('[name=q]').value">Child submit</button>
<p id=out></p>
<iframe src="https://grandchild.test/" width=300 height=90></iframe>""",
    "https://grandchild.test/": """<!doctype html>
<button onclick="this.textContent = 'Nested clicked'">Nested</button>""",
    "https://child.test/opaque": """<!doctype html><body style="margin:0">
<div id=host style="width:300px;height:80px"></div>
<script>
  const root = host.attachShadow({mode: 'closed'});
  const b = document.createElement('button');
  b.textContent = 'Verify';
  b.style.cssText = 'width:300px;height:80px';
  b.onclick = () => { document.body.dataset.verified = '1'; };
  root.appendChild(b);
</script></body>""",
    "https://child.test/covered": """<!doctype html><button>Hidden behind overlay</button>""",
}


def _frame(page, url):
    return next(f for f in page.frames if f.url == url)


def _index(snap, text):
    return next(el.index for el in snap.elements if el.text == text)


def test_frames_are_indexed_and_actionable():
    with launch(engine="camoufox", headless=True, geoip=False) as session:
        session.context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=PAGES.get(route.request.url, "")
            ),
        )
        ab = AgentBrowser(session=session)
        snap = ab.navigate("https://parent.test/")
        session.page.wait_for_timeout(500)
        snap = ab.snapshot()
        texts = {el.text: el for el in snap.elements}

        assert texts["Parent button"].frame == 0
        assert texts["Child submit"].frame > 0
        assert texts["Nested"].frame > texts["Child submit"].frame
        assert "Hidden behind overlay" not in texts
        opaque = [f for f in snap.frames if f.url.endswith("/opaque")]
        assert opaque and opaque[0].element_count == 0
        assert "(frame " in snap.to_text() and "click_frame(" in snap.to_text()

        query = next(el.index for el in snap.elements if el.attributes.get("name") == "q")
        ab.type(query, "frames work")
        ab.click(_index(ab.last_snapshot, "Child submit"))
        out = _frame(session.page, "https://child.test/form").evaluate("document.getElementById('out').textContent")
        assert out == "submitted:frames work"

        ab.click(_index(ab.last_snapshot, "Nested"))
        nested = _frame(session.page, "https://grandchild.test/")
        assert nested.evaluate("document.querySelector('button').textContent") == "Nested clicked"

        box = next(f for f in ab.last_snapshot.frames if f.url == opaque[0].url)
        verified = "document.body.dataset.verified === '1'"
        ab.click_at(box.x + box.width / 2, box.y + box.height / 2)
        assert _frame(session.page, "https://child.test/opaque").evaluate(verified)

        _frame(session.page, "https://child.test/opaque").evaluate(
            "delete document.body.dataset.verified"
        )
        number = next(f.number for f in ab.last_snapshot.frames if f.url == opaque[0].url)
        ab.click_frame(number, 0.5, 0.5)
        assert _frame(session.page, "https://child.test/opaque").evaluate(verified)

        with pytest.raises(SecretPolicyError, match="main-frame"):
            ab.fill_secret(query, {
                "provider": "none",
                "handle": "opaque",
                "allowed_origins": ["https://parent.test"],
                "field_kind": "text",
            })
