"""Opt-in launch check: WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_launch.py."""

from __future__ import annotations

import os

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip("set WRAITH_LIVE_TEST=1 to launch Camoufox", allow_module_level=True)

from wraith.engine import launch


def test_pinned_camoufox_launches_and_renders():
    with launch(engine="camoufox", headless=True, geoip=False) as session:
        session.page.set_content("<p id=ok>wraith</p>")

        assert session.engine == "camoufox"
        assert session.page.text_content("#ok") == "wraith"
        assert "Firefox/158" in session.page.evaluate("navigator.userAgent")
