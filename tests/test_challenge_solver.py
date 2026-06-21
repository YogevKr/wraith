"""Offline tests for the vendor-dispatching Challenge abstraction."""
from __future__ import annotations

import pytest

from wraith.recaptcha import CapSolver, Challenge, TwoCaptcha


def test_from_vendor_mapping():
    assert Challenge.from_vendor("cloudflare", "SK", "u").kind == "turnstile"
    assert Challenge.from_vendor("hCaptcha", "SK", "u").kind == "hcaptcha"
    assert Challenge.from_vendor("reCAPTCHA", "SK", "u").kind == "recaptcha_v3"
    assert Challenge.from_vendor("aws-waf", "SK", "u").kind == "awswaf"
    assert Challenge.from_vendor("totally-unknown", "SK", "u").kind == "recaptcha_v3"


def test_capsolver_task_for_turnstile():
    task, field = CapSolver("k")._task_for(
        Challenge("turnstile", "SK", "https://x", action="login", data="CD")
    )
    assert task["type"] == "AntiTurnstileTaskProxyLess" and field == "token"
    assert task["metadata"] == {"action": "login", "cdata": "CD"}


def test_capsolver_task_for_v3_enterprise():
    task, field = CapSolver("k")._task_for(
        Challenge("recaptcha_v3", "SK", "u", action="submit", enterprise=True, min_score=0.7)
    )
    assert task["type"] == "ReCaptchaV3TaskProxyLess" and field == "gRecaptchaResponse"
    assert task["pageAction"] == "submit" and task["minScore"] == 0.7
    assert task["isEnterprise"] is True


def test_twocaptcha_task_for_hcaptcha_and_turnstile():
    task, field = TwoCaptcha("k")._task_for(Challenge("hcaptcha", "SK", "u"))
    assert task["type"] == "HCaptchaTaskProxyless" and field == "gRecaptchaResponse"
    t2, f2 = TwoCaptcha("k")._task_for(Challenge("turnstile", "SK", "u", action="a", data="d"))
    assert t2["type"] == "TurnstileTaskProxyless" and t2["action"] == "a" and t2["data"] == "d"


def test_task_for_unsupported_kind():
    with pytest.raises(ValueError):
        CapSolver("k")._task_for(Challenge("datadome", "SK", "u"))


def test_solve_challenge_happy_path(monkeypatch):
    import httpx

    class FakeResp:
        def __init__(self, data):
            self._d = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._d

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            if url.endswith("/createTask"):
                return FakeResp({"taskId": "T1"})
            return FakeResp({"status": "ready", "solution": {"token": "TOK123"}})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    tok = CapSolver("k").solve_challenge(Challenge("turnstile", "SK", "u"))
    assert tok == "TOK123"


def test_challenge_exported():
    import wraith
    assert hasattr(wraith, "Challenge")
