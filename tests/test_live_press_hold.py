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
