"""Offline tests for the stealth self-test harness (normalization + verdict)."""
from __future__ import annotations

from wraith import detect


def test_normalize_status():
    n = detect._normalize_status
    assert n("passed") == "pass"
    assert n("✅ ok") == "pass"
    assert n("FAILED (leak detected)") == "fail"
    assert n("bot") == "fail"
    assert n("warn") == "warn"
    assert n(None) == "unknown"
    assert n("") == "unknown"
    assert n("mystery") == "unknown"


def test_selftest_passed(monkeypatch):
    monkeypatch.setattr(detect, "bot_detector", lambda page: {
        "runtimeEnableLeak": "pass",
        "navigatorWebdriver": "pass",
        "pwInitScripts": "pass",
        "sourceUrlLeak": "pass",
        "dummyFn": "pass",
        "viewport": "warn",
    })
    r = detect.selftest(object())
    assert r["passed"] is True
    assert r["critical_failures"] == []
    assert r["checks"]["viewport"]["status"] == "warn"


def test_selftest_critical_fail(monkeypatch):
    monkeypatch.setattr(detect, "bot_detector", lambda page: {
        "runtimeEnableLeak": "fail (leak)",
        "navigatorWebdriver": "pass",
    })
    r = detect.selftest(object())
    assert r["passed"] is False
    assert "runtimeEnableLeak" in r["critical_failures"]
    assert "runtimeEnableLeak" in r["failures"]


def test_selftest_exported():
    import wraith
    assert hasattr(wraith, "selftest")
