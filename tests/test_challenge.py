"""Offline tests for hard-block detection and solved-token injection."""
from __future__ import annotations

from wraith import detect, recaptcha


def test_is_blocked_hard_blocks():
    assert detect.is_blocked("<html>... Error 1020 ...</html>")
    assert detect.is_blocked("body text", title="Sorry, you have been blocked")
    assert detect.is_blocked("This request was blocked by our security service")
    assert detect.is_blocked("Access to this page has been denied")


def test_is_blocked_ignores_challenge_and_normal():
    # An interactive JS challenge is NOT a hard block — it may still clear.
    assert detect.is_blocked("<title>Just a moment...</title> challenge-platform") is None
    assert detect.is_blocked("<html><body>Welcome back</body></html>") is None
    assert detect.is_blocked("") is None


def test_inject_token_writes_default_fields():
    captured: dict = {}

    class FakePage:
        def evaluate(self, js, args):
            captured["js"] = js
            captured["args"] = args
            return ["g-recaptcha-response"]  # simulate that field existed

    out = recaptcha.inject_token(FakePage(), "TOK")
    assert out == ["g-recaptcha-response"]
    assert captured["args"]["token"] == "TOK"
    assert "cf-turnstile-response" in captured["args"]["fields"]
    assert captured["args"]["field"] is None


def test_inject_token_explicit_field():
    captured: dict = {}

    class FakePage:
        def evaluate(self, js, args):
            captured.update(args)
            return [args["field"]]

    out = recaptcha.inject_token(FakePage(), "T", field="cf-turnstile-response")
    assert out == ["cf-turnstile-response"]
    assert captured["field"] == "cf-turnstile-response"


def test_wraith_exports_batch2():
    import wraith

    assert hasattr(wraith, "is_blocked")
    assert hasattr(wraith, "inject_token")
