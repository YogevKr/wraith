"""Opt-in stealth checks on local pages: WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_fingerprint.py.

No network: a server on 127.0.0.1 serves a fingerprint probe, an input probe
and a header echo. Page scripts write what they see into a DOM attribute,
because Wraith's own evaluate runs in an isolated world.
"""

from __future__ import annotations

import http.server
import json
import os
import re

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip(
        "set WRAITH_LIVE_TEST=1 to run the fingerprint checks", allow_module_level=True
    )

from tests._local_server import serve
from wraith.agent import AgentBrowser
from wraith.engine import launch

ENGINES = ["camoufox", "chromium"]
TYPED = "Probe Text 42!"
SOFTWARE_GL = re.compile(r"SwiftShader|llvmpipe|softpipe", re.I)

PROBE_PAGE = """<!doctype html><title>probe</title><body><p>probe</p><script>
(async () => {
  const out = {webdriver: navigator.webdriver, ua: navigator.userAgent, chrome: typeof window.chrome,
    brands: navigator.userAgentData ? navigator.userAgentData.brands.map(b => b.brand) : null,
    languages: navigator.languages, plugins: navigator.plugins.length,
    screen: [screen.width, screen.height], inner: [innerWidth, innerHeight], outer: [outerWidth, outerHeight]};
  const worker = new Worker(URL.createObjectURL(new Blob(['postMessage(navigator.userAgent)'])));
  out.workerUA = await new Promise(resolve => { worker.onmessage = e => resolve(e.data); setTimeout(() => resolve(null), 5000); });
  out.headers = await (await fetch('/headers')).json();
  const gl = document.createElement('canvas').getContext('webgl');
  const info = gl && gl.getExtension('WEBGL_debug_renderer_info');
  out.webgl = gl ? gl.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : gl.RENDERER) : null;
  document.body.dataset.probe = JSON.stringify(out);
})();
</script></body>"""

INPUT_PAGE = """<!doctype html><title>input</title><body>
<p>Input probe page with a field and a button below the fold.</p>
<div style="height: 1400px"></div>
<input name=field placeholder="Probe field" autocomplete=off>
<button id=target>Probe target</button>
<script>
const probe = {events: [], moves: 0, added: 0, attributes: 0};
const save = () => {
  // What the page itself can see of the agent: marked nodes, its globals.
  probe.marked = Array.from(document.querySelectorAll('*')).filter(
    el => Array.from(el.attributes).some(a => a.name.startsWith('data-wraith'))).length;
  probe.globals = Object.keys(window).filter(k => /wraith/i.test(k));
  document.documentElement.dataset.input = JSON.stringify(probe);
};
for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'keydown', 'keyup']) {
  document.addEventListener(type, e => {
    probe.events.push({type, trusted: e.isTrusted, key: e.key || null, time: performance.now()});
    save();
  }, true);
}
document.addEventListener('mousemove', () => { probe.moves += 1; save(); }, true);
new MutationObserver(records => {
  for (const r of records) {
    probe.added += r.addedNodes.length;
    if (r.type === 'attributes') probe.attributes += 1;
  }
  save();
}).observe(document.body, {childList: true, subtree: true, attributes: true});
save();
</script></body>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/headers":
            body, ctype = (
                json.dumps({k.lower(): v for k, v in self.headers.items()}),
                "application/json",
            )
        else:
            body, ctype = (
                (INPUT_PAGE if self.path == "/input" else PROBE_PAGE),
                "text/html",
            )
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def base():
    with serve(_Handler) as server:
        yield f"http://127.0.0.1:{server.server_port}"


def _dataset(page, root, key):
    script = f"() => {root}.dataset.{key} || null"
    page.wait_for_function(script, timeout=15000)
    return json.loads(page.evaluate(script))


@pytest.mark.parametrize("engine", ENGINES)
def test_fingerprint_has_no_automation_tells(base, engine):
    with launch(engine=engine, headless=True, geoip=False) as session:
        session.page.goto(f"{base}/probe")
        seen = _dataset(session.page, "document.body", "probe")

    ua = seen["ua"]
    assert seen["webdriver"] is not True
    for value in (
        ua,
        seen["workerUA"],
        seen["headers"]["user-agent"],
        seen["headers"].get("sec-ch-ua", ""),
    ):
        assert "HeadlessChrome" not in value, seen
    assert seen["workerUA"] == ua and seen["headers"]["user-agent"] == ua
    assert seen["languages"] and seen["plugins"] > 0
    assert seen["webgl"] and not SOFTWARE_GL.search(seen["webgl"]), seen["webgl"]
    (sw, sh), (ow, oh), (iw, ih) = seen["screen"], seen["outer"], seen["inner"]
    assert sw >= ow >= iw and sh >= oh >= ih, seen
    assert (iw, ih) != (1280, 720), "Playwright's default viewport"
    if engine == "chromium":
        assert seen["chrome"] == "object"
        assert (
            "HeadlessChrome" not in seen["brands"] and "Google Chrome" in seen["brands"]
        )


@pytest.mark.parametrize("engine", ENGINES)
def test_agent_input_looks_like_a_person(base, engine):
    with launch(engine=engine, headless=True, geoip=False) as session:
        ab = AgentBrowser(session=session)
        snap = ab.navigate(f"{base}/input")
        for _ in range(4):  # the button is below the fold, as an agent would find it
            if any(el.text == "Probe target" for el in snap.elements):
                break
            snap = ab.scroll("down")
        button = next(el.index for el in snap.elements if el.text == "Probe target")
        ab.click(button)
        field = next(
            el.index
            for el in ab.last_snapshot.elements
            if el.attributes.get("name") == "field"
        )
        ab.type(field, TYPED)
        probe = _dataset(session.page, "document.documentElement", "input")
        value = session.page.evaluate(
            "() => document.querySelector('[name=field]').value"
        )

    events = probe["events"]
    mouse = [
        e
        for e in events
        if e["type"] in ("pointerdown", "mousedown", "pointerup", "mouseup", "click")
    ]
    first_click = mouse[
        : mouse.index(next(e for e in mouse if e["type"] == "click")) + 1
    ]
    assert [e["type"] for e in first_click] == [
        "pointerdown",
        "mousedown",
        "pointerup",
        "mouseup",
        "click",
    ]
    assert all(e["trusted"] for e in events), events
    assert probe["moves"] > 0, "the mouse jumped to the target"
    held = first_click[3]["time"] - first_click[1]["time"]
    assert held > 5, f"the button was held for {held:.1f} ms"
    keydowns = [
        e["key"] for e in events if e["type"] == "keydown" and e["key"] in TYPED
    ]
    assert "".join(keydowns).endswith(TYPED)
    downs = {}
    holds = []
    for e in events:
        if e["type"] == "keydown":
            downs[e["key"]] = e["time"]
        elif e["type"] == "keyup" and e["key"] in downs:
            holds.append(e["time"] - downs.pop(e["key"]))
    assert len(holds) >= len(TYPED) and sorted(holds)[len(holds) // 2] > 5, holds
    assert value == TYPED
    assert probe["added"] == 0, "Wraith added nodes to the page"
    assert probe["attributes"] == 0 and probe["marked"] == 0, "Wraith marked page nodes"
    assert probe["globals"] == [], probe["globals"]


def test_chromium_command_line_has_no_automation_flags():
    with launch(engine="chromium", headless=True, geoip=False) as session:
        session.page.goto("chrome://version")
        command_line = session.page.inner_text("#command_line")

    flags = {
        flag.split("=")[0] for flag in command_line.split() if flag.startswith("--")
    }
    banned = {
        "--enable-automation",
        "--enable-unsafe-swiftshader",
        "--no-sandbox",
        "--disable-gpu",
        "--remote-debugging-port",
        "--single-process",
        "--disable-web-security",
    }
    assert not flags & banned, sorted(flags & banned)
    assert "--headless=old" not in command_line
