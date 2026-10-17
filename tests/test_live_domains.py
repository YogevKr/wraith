"""Opt-in allowlist check: WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_domains.py.

One local server answers on two host names: 127.0.0.1 is allowed and
localhost is not. The server counts every request that reaches it.
"""

from __future__ import annotations

import http.server
import os

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip("set WRAITH_LIVE_TEST=1 to run the allowlist check", allow_module_level=True)

from tests._local_server import serve
from wraith.agent import AgentBrowser
from wraith.domains import DomainNotAllowedError
from wraith.engine import launch

PAD = "<p>" + "Fixture text for a page with real content. " * 8 + "</p>"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits.append((self.headers.get("Host", "").split(":")[0], self.path))
        port = self.server.server_port
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{port}/landing")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        body = (
            f'<img src="http://localhost:{port}/pixel.png">'
            f'<script>fetch("http://localhost:{port}/api?leak=1").catch(() => {{}})</script>'
            f'<iframe src="http://localhost:{port}/frame" width=300 height=80></iframe>'
            f'<a id=out href="http://localhost:{port}/landing?leak=1">Leave</a>{PAD}'
        )
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    with serve(_Handler) as srv:
        srv.hits = []
        yield srv


def test_allowlist_keeps_the_browser_on_allowed_hosts(server):
    port = server.server_port
    home = f"http://127.0.0.1:{port}/page"
    with launch(engine="camoufox", headless=True, geoip=False) as session:
        ab = AgentBrowser(session=session, allowed_domains="127.0.0.1")

        snap = ab.navigate(home)
        session.page.wait_for_timeout(500)
        assert not snap.frames  # the localhost iframe is refused

        link = next(el.index for el in ab.snapshot().elements if el.text == "Leave")
        with pytest.raises(DomainNotAllowedError, match="Blocked a navigation: localhost"):
            ab.click(link)
        assert session.page.url == home

        with pytest.raises(DomainNotAllowedError, match="localhost"):
            ab.navigate(f"http://localhost:{port}/direct?leak=1")

        with pytest.raises(DomainNotAllowedError, match="redirect"):
            ab.navigate(f"http://127.0.0.1:{port}/redirect")
        assert session.page.url == "about:blank"

    reached = {(host, path) for host, path in server.hits if host == "localhost"}
    # Only the server redirect reaches the refused host; Wraith then closes it.
    assert reached == {("localhost", "/landing")}
