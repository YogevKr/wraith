"""Opt-in network log check: WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_netlog.py.

A page on 127.0.0.1 calls a JSON API, loads an image, and calls localhost,
which the domain allowlist refuses.
"""

from __future__ import annotations

import http.server
import json
import os

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip(
        "set WRAITH_LIVE_TEST=1 to run the network log check", allow_module_level=True
    )

from tests._local_server import serve
from wraith.agent import AgentBrowser
from wraith.engine import launch

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082"
)
PAD = "<p>" + "Fixture text for a page with real content. " * 8 + "</p>"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        port = self.server.server_port
        if self.path.startswith("/api/items"):
            body, ctype = (
                json.dumps({"items": ["alpha", "beta"]}).encode(),
                "application/json",
            )
        elif self.path == "/logo.png":
            body, ctype = PNG, "image/png"
        else:
            body = (
                f"<!doctype html><title>App</title>{PAD}<img src=/logo.png>"
                f"<script>fetch('/api/items?page=1');"
                f"fetch('http://localhost:{port}/track').catch(() => {{}});</script>"
            ).encode()
            ctype = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("engine", ["camoufox", "chromium"])
def test_network_log_finds_the_api_and_its_body(engine):
    with (
        serve(_Handler) as server,
        launch(engine=engine, headless=True, geoip=False) as session,
    ):
        base = f"http://127.0.0.1:{server.server_port}"
        ab = AgentBrowser(session=session, allowed_domains="127.0.0.1")
        ab.navigate(f"{base}/app")
        session.page.wait_for_timeout(500)

        (api,) = ab.network(contains="/api/items")
        assert (api["status"], api["type"]) == (200, "fetch")
        assert api["content_type"].startswith("application/json")
        assert json.loads(ab.network_response(api["id"])) == {
            "items": ["alpha", "beta"]
        }

        (logo,) = ab.network(contains="/logo.png")
        assert (
            ab.network_response(logo["id"])
            == f"(binary body: {len(PNG)} bytes, image/png)"
        )

        (blocked,) = ab.network(contains="localhost")
        assert blocked["failure"] and blocked["status"] is None
        assert ab.network(resource_type="document")[0]["url"] == f"{base}/app"
