"""Opt-in browser checks using local fixtures; no live challenge service."""

from __future__ import annotations

import os

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip(
        "set WRAITH_LIVE_TEST=1 to run Camoufox checks", allow_module_level=True
    )

from wraith import _press_hold
from wraith.engine import clear_challenge, launch

URL = "https://press-hold.test/"
BUTTON = """
<div id="px-captcha" style="display: inline-block"></div>
<script>
const host = document.querySelector('#px-captcha');
const root = SHADOW ? host.attachShadow({mode: SHADOW}) : host;
const button = document.createElement(TAG);
button.textContent = LABEL;
root.appendChild(button);
let start = 0;
button.addEventListener('mousedown', event => {
    if (event.isTrusted) start = performance.now();
    button.textContent = 'Keep holding';
});
button.addEventListener('mouseup', event => {
    if (event.isTrusted && start && performance.now() - start >= 50) {
        window.parent.postMessage('verified', '*');
    }
});
</script>
"""


@pytest.mark.parametrize("mode", ["page", "iframe", "shadow", "closed", "plain"])
def test_real_mouse_events_clear_local_challenge(monkeypatch, mode):
    # Keep browser tests short; unit tests verify the production eight-second cap.
    monkeypatch.setattr(_press_hold, "_HOLD_SECONDS", 0.2)
    shadow = {"shadow": "'open'", "closed": "'closed'"}.get(mode, "null")
    button = (
        BUTTON.replace("SHADOW", shadow)
        .replace("TAG", "'div'" if mode == "plain" else "'button'")
        .replace("LABEL", "'Hold here'" if mode == "plain" else "'Press & Hold'")
    )
    control = (
        '<iframe src="https://hold-frame.test/"></iframe>'
        if mode == "iframe"
        else button
    )
    challenge = (
        "<title>Access to this page has been denied</title>"
        "<p>Press &amp; Hold to confirm you are a human (and not a bot).</p>"
        + control
        + """<script>
        window.addEventListener('message', event => {
            if (event.data === 'verified') location.href = '/cleared';
        });
        </script>"""
    )
    cleared = "<h1>Verified catalog</h1>" + "<p>Product details</p>" * 30

    with launch(engine="camoufox", headless=True, geoip=False) as session:

        def respond(route):
            url = route.request.url
            if url == "https://hold-frame.test/":
                route.fulfill(status=200, content_type="text/html", body=button)
            elif url == URL + "cleared":
                route.fulfill(status=200, content_type="text/html", body=cleared)
            else:
                route.fulfill(status=403, content_type="text/html", body=challenge)

        session.context.route("**/*", respond)
        assert clear_challenge(URL, session=session, timeout=10, settle=0.1) is session
        assert session.page.url == URL + "cleared"
        assert session.page.get_by_role("heading", name="Verified catalog").is_visible()


SLOW_CHALLENGE = """<title>Access to this page has been denied</title>
<p>Press &amp; Hold to confirm you are a human (and not a bot).</p>
<div id="px-captcha" style="display:inline-block"></div>
<script>
const b = document.createElement('div'); b.textContent = 'Hold here';
document.querySelector('#px-captcha').appendChild(b);
let start = 0;
b.addEventListener('mousedown', e => { if (e.isTrusted) start = performance.now(); });
b.addEventListener('mouseup', e => {
    if (e.isTrusted && start && performance.now() - start >= 50) location.href = '/cleared';
});
</script>"""


def test_a_hold_that_clears_slowly_is_not_a_hard_block(monkeypatch):
    """CI flake: the cleared page committed while the hold check ran, and the
    content read before it (still "access denied") was called a hard block."""
    import http.server
    import time

    from tests._local_server import serve

    monkeypatch.setattr(_press_hold, "_HOLD_SECONDS", 0.2)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/cleared":
                time.sleep(0.5)  # a slow server
                body, status = "<h1>Verified catalog</h1>" + "<p>Product</p>" * 30, 200
            else:
                body, status = SLOW_CHALLENGE, 403
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    real_inspect = _press_hold.inspect_press_hold
    calls = []

    def slow_inspect(page, html):  # a slow machine: the check takes 0.6 s
        calls.append(html)
        if len(calls) > 1:
            page.wait_for_timeout(600)
        return real_inspect(page, html)

    monkeypatch.setattr(_press_hold, "inspect_press_hold", slow_inspect)
    with serve(Handler) as server, launch(engine="camoufox", headless=True, geoip=False) as session:
        url = f"http://127.0.0.1:{server.server_port}/"
        assert clear_challenge(url, session=session, timeout=10, settle=0.1) is session
        assert session.page.url == url + "cleared"
