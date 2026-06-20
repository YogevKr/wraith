"""Smoke tests for Wraith.

These run offline (no browser binaries, no network). They assert that:

* the package imports cleanly with no missing sub-modules,
* the documented public API symbols exist on the package, and
* the pure-logic core (``identify_waap`` over a fake header/cookie set)
  works without launching a browser.
"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# Import + public API surface
# --------------------------------------------------------------------------- #

def test_import_wraith():
    import wraith

    assert wraith.__version__ == "0.1.0"
    assert isinstance(wraith.__all__, list)


def test_no_missing_imports():
    """All sub-modules should import cleanly in the dev environment."""
    import wraith

    assert wraith.missing_imports == {}, (
        f"sub-modules failed to import: {wraith.missing_imports}"
    )


# The full public API the integrator re-exports from the sub-modules.
EXPECTED_API = [
    # engine
    "launch",
    "browser",
    "Session",
    "Engine",
    "WraithEngineError",
    "EngineUnavailableError",
    "PlaywrightVersionError",
    "playwright_version",
    # identity
    "Cookie",
    "find_firefox_profiles",
    "find_zen_profiles",
    "find_chrome_profile",
    "extract_cookies",
    "to_playwright_cookies",
    "inject_cookies",
    "ChromeEncryptionError",
    # harvest
    "SessionHarvester",
    "CapturedSession",
    "harvest_session",
    # detect
    "recaptcha_v3_score",
    "bot_detector",
    "identify_waap",
    "fingerprint",
    "cookie_is_valid",
    "CLEARANCE_COOKIES",
    "RECAPTCHA_V3_TEST_URL",
    "BOT_DETECTOR_URL",
    # proxy
    "ProxyPool",
    "normalize_proxy",
    # behavior
    "human_move",
    "human_type",
    "dwell",
]


@pytest.mark.parametrize("name", EXPECTED_API)
def test_public_symbol_exists(name):
    import wraith

    assert hasattr(wraith, name), f"wraith.{name} is missing"
    assert name in wraith.__all__, f"{name} not declared in wraith.__all__"


def test_key_callables_are_callable():
    import wraith

    for name in (
        "launch",
        "browser",
        "extract_cookies",
        "inject_cookies",
        "harvest_session",
        "identify_waap",
        "recaptcha_v3_score",
        "human_move",
    ):
        assert callable(getattr(wraith, name)), f"{name} should be callable"


# --------------------------------------------------------------------------- #
# identify_waap pure-logic path (offline, no browser)
# --------------------------------------------------------------------------- #

def test_identify_waap_offline_with_fake_httpx_response():
    """identify_waap should fingerprint vendors from a fake header/cookie set.

    We feed it a synthetic ``httpx.Response`` so no network call happens. The
    headers/cookies below mimic EL AL's observed stack: Reblaze/Link11
    (rhino-core-shield server + rbzid cookie), Akamai (_abck cookie) and
    SiteMinder (SMSESSION cookie).
    """
    import httpx

    resp = httpx.Response(
        status_code=200,
        headers=[
            ("server", "rhino-core-shield"),
            ("set-cookie", "rbzid=abc123; Path=/; Secure; HttpOnly"),
            ("set-cookie", "_abck=def456~0~-1~-1; Path=/"),
            ("set-cookie", "SMSESSION=ghi789; Path=/"),
        ],
        request=httpx.Request("GET", "https://www.example.com/"),
    )

    vendors = wraith_identify(resp)

    assert "Reblaze/Link11" in vendors
    assert "Akamai" in vendors
    assert "SiteMinder" in vendors


def test_identify_waap_empty_on_clean_response():
    import httpx

    resp = httpx.Response(
        status_code=200,
        headers=[("server", "nginx"), ("content-type", "text/html")],
        request=httpx.Request("GET", "https://example.org/"),
    )
    assert wraith_identify(resp) == []


def test_identify_waap_recaptcha_from_body():
    """A grecaptcha tell in the body alone should flag reCAPTCHA."""
    import httpx

    resp = httpx.Response(
        status_code=200,
        headers=[("content-type", "text/html")],
        content=b"<script src='https://www.google.com/recaptcha/api.js'></script>",
        request=httpx.Request("GET", "https://example.net/"),
    )
    assert "reCAPTCHA" in wraith_identify(resp)


def wraith_identify(resp):
    """Helper: call the re-exported wraith.identify_waap."""
    import wraith

    return wraith.identify_waap(resp)
