"""Live detector check: WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_selftest.py."""

from __future__ import annotations

import os

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip("set WRAITH_LIVE_TEST=1 to run the detector check", allow_module_level=True)

from wraith.detect import _BOT_DETECTOR_TESTS, selftest
from wraith.engine import launch


def test_live_detector_returns_measurements():
    proxy = os.environ.get("WRAITH_E2E_PROXY")
    with launch(engine="camoufox", headless=True, geoip=False, proxy=proxy) as session:
        result = selftest(session.page)

    raw = result["checks"]
    assert all(raw[name]["raw"] is not None for name in _BOT_DETECTOR_TESTS)
    assert any(raw[name]["status"] in {"pass", "fail"} for name in _BOT_DETECTOR_TESTS)
