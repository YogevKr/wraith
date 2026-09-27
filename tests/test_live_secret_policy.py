"""Opt-in Camoufox checks for the browser secret policy.

Run with ``WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_secret_policy.py``.
The page is local, so the test does not need a WAAP or a network target.
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip(
        "set WRAITH_LIVE_TEST=1 to run Camoufox checks", allow_module_level=True
    )

from wraith.agent import AgentBrowser
from wraith.engine import launch
from wraith.secrets import (
    SecretCapability,
    SecretCapabilityError,
    SecretMaterial,
    SecretPolicyError,
)


CANARY = "wraith-local-canary-7f3a"
PAGE = """<!doctype html>
<html><body>
  <label for="username">User</label>
  <input id="username" name="username" autocomplete="username" type="text">
  <label for="password">Password</label>
  <input id="password" name="password" autocomplete="current-password" type="password">
</body></html>
"""


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class _CanaryProvider:
    def __init__(self):
        self.calls = 0
        self.material = None

    def resolve(self, _capability, _context):
        self.calls += 1
        self.material = SecretMaterial(CANARY)
        return self.material


def _serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/login"


def _string_hash(value: str) -> str:
    """Return the same string-only hash used inside the Camoufox page."""
    digest = 2166136261
    for char in value:
        digest ^= ord(char)
        digest = (digest * 16777619) & 0xFFFFFFFF
    return f"{digest:08x}"


def _hash_dom_string(page, selector: str) -> str:
    return page.evaluate(
        """({selector}) => {
          const value = document.querySelector(selector)?.value || "";
          let digest = 2166136261;
          for (let i = 0; i < value.length; i += 1) {
            digest ^= value.charCodeAt(i);
            digest = Math.imul(digest, 16777619);
          }
          return (digest >>> 0).toString(16).padStart(8, "0");
        }""",
        {"selector": selector},
    )


def _capability(origin: str, *, field_kind: str = "password") -> SecretCapability:
    return SecretCapability(
        provider="canary",
        handle="local-canary-handle",
        allowed_origins=(origin,),
        field_kind=field_kind,
        max_uses=1,
    )


def _field_index(browser: AgentBrowser, field_type: str) -> int:
    snapshot = browser.snapshot()
    return next(
        element.index
        for element in snapshot
        if element.attributes.get("type") == field_type
    )


def test_camoufox_secret_policy_uses_direct_navigation_and_string_hashes(
    tmp_path: Path,
):
    first_server, first_url = _serve()
    second_server, second_url = _serve()
    try:
        with launch(engine="camoufox", headless=True, geoip=False) as session:
            # Keep local tests out of AgentBrowser.navigate(), which waits for
            # a WAAP clearance cookie before it accepts a page.
            session.page.goto(second_url, wait_until="domcontentloaded")
            provider = _CanaryProvider()
            browser = AgentBrowser(
                session=session,
                secret_providers={"canary": provider},
            )
            password_index = _field_index(browser, "password")

            with pytest.raises(SecretPolicyError, match="origin"):
                browser.fill_secret(
                    password_index, _capability(first_url.rsplit("/", 1)[0])
                )
            assert provider.calls == 0

            session.page.goto(first_url, wait_until="domcontentloaded")
            password_index = _field_index(browser, "password")
            with pytest.raises(SecretPolicyError, match="field_kind"):
                browser.fill_secret(
                    password_index,
                    _capability(first_url.rsplit("/", 1)[0], field_kind="username"),
                )
            assert provider.calls == 0

            browser.fill_secret(
                password_index, _capability(first_url.rsplit("/", 1)[0])
            )
            assert provider.calls == 1
            assert provider.material is not None and provider.material.cleared

            assert _hash_dom_string(session.page, "#password") == _string_hash(CANARY)
            assert CANARY not in session.page.content()
            assert CANARY not in browser.snapshot().to_text()
            assert browser.secret_tainted is True

            with pytest.raises(SecretCapabilityError, match="exhausted"):
                browser.fill_secret(
                    password_index, _capability(first_url.rsplit("/", 1)[0])
                )
            with pytest.raises(SecretPolicyError, match="Storage export"):
                browser.save_storage_state(str(tmp_path / "state.json"))
    finally:
        first_server.shutdown()
        second_server.shutdown()
        first_server.server_close()
        second_server.server_close()
