"""Opt-in handoff check: WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_handoff.py.

Needs Camoufox and patchright Chromium. A local server sets a cookie with no
SameSite attribute in Camoufox; Chromium must send it back after the handoff.
"""

from __future__ import annotations

import http.server
import os
import threading

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip("set WRAITH_LIVE_TEST=1 to run the handoff check", allow_module_level=True)

from wraith.engine import launch
from wraith.identity import export_storage_state


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if self.path == "/set":
            self.send_header("Set-Cookie", "sid=abc123; Path=/; HttpOnly")
            body = "<script>localStorage.setItem('token', 'xyz789')</script>"
        else:
            body = (
                f"<p id=c>{self.headers.get('Cookie', '')}</p><p id=l></p>"
                "<script>document.getElementById('l').textContent ="
                " localStorage.getItem('token')</script>"
            )
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def origin():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_camoufox_session_loads_in_chromium(origin, tmp_path):
    state = str(tmp_path / "state.json")
    with launch(engine="camoufox", headless=True, geoip=False) as firefox:
        firefox.page.goto(f"{origin}/set")
        firefox.page.wait_for_function("localStorage.getItem('token') !== null")
        export_storage_state(firefox.context, state, for_chromium=True)

    with launch(engine="chromium", headless=True, geoip=False, storage_state=state) as chromium:
        chromium.page.goto(f"{origin}/check")

        assert chromium.page.text_content("#c") == "sid=abc123"
        assert chromium.page.text_content("#l") == "xyz789"
