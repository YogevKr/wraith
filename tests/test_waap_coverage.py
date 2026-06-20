"""Offline multi-vendor WAAP detection coverage.

Every test here is network-free and browser-free: we craft synthetic
``httpx.Response`` objects whose headers / set-cookie / body mimic each
vendor's tell-tale signals, then assert that
:func:`wraith.detect.identify_waap` flags the right vendor, that
:func:`wraith.detect.cookie_is_valid` enforces the Akamai ``_abck``
solved-state rule, and that :func:`wraith.detect.fingerprint` returns the
agreed structured shape (per-vendor ``tier`` / ``strategy``).
"""

from __future__ import annotations

import httpx
import pytest

from wraith.detect import (
    CLEARANCE_COOKIES,
    cookie_is_valid,
    fingerprint,
    identify_waap,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _resp(
    *,
    headers: dict[str, str] | None = None,
    set_cookies: list[str] | None = None,
    status: int = 200,
    body: str = "",
    url: str = "https://target.test/",
) -> httpx.Response:
    """Build a synthetic httpx.Response (no network)."""
    raw: list[tuple[str, str]] = list((headers or {}).items())
    for cookie in set_cookies or []:
        raw.append(("set-cookie", cookie))
    return httpx.Response(
        status,
        headers=raw,
        text=body,
        request=httpx.Request("GET", url),
    )


# --------------------------------------------------------------------------- #
# Per-vendor detection — one crafted response per vendor
# --------------------------------------------------------------------------- #

# (label, kwargs-for-_resp, expected vendor name in identify_waap output)
VENDOR_CASES = [
    (
        "cloudflare",
        dict(
            headers={"cf-ray": "8a1b2c3d4e5f-TLV", "server": "cloudflare"},
            set_cookies=["cf_clearance=abcdef; Path=/; Secure"],
        ),
        "Cloudflare",
    ),
    (
        "akamai",
        dict(set_cookies=["_abck=7A2B~0~xyz; Path=/", "bm_sz=foobar; Path=/"]),
        "Akamai",
    ),
    (
        "datadome",
        dict(set_cookies=["datadome=zZ9qabc123; Path=/; Secure; SameSite=Lax"]),
        "DataDome",
    ),
    (
        "perimeterx",
        dict(set_cookies=["_px=token; Path=/", "_pxvid=visitor; Path=/"]),
        "PerimeterX/HUMAN",
    ),
    (
        "kasada",
        dict(headers={"x-kpsdk-ct": "ct-token-value"}),
        "Kasada",
    ),
    (
        "incapsula",
        dict(
            set_cookies=[
                "visid_incap_1234567=base64stuff; Path=/",
                "incap_ses_123_1234567=sessionval; Path=/",
            ]
        ),
        "Imperva/Incapsula",
    ),
    (
        "reblaze",
        dict(
            headers={"server": "rhino-core-shield"},
            set_cookies=["waap_id=abc; Path=/", "rbzid=def; Path=/"],
        ),
        "Reblaze/Link11",
    ),
    (
        "aws_waf",
        dict(set_cookies=["aws-waf-token=11111111-2222-3333; Path=/"]),
        "AWS WAF",
    ),
]


@pytest.mark.parametrize(
    "label,kwargs,expected", VENDOR_CASES, ids=[c[0] for c in VENDOR_CASES]
)
def test_identify_waap_detects_vendor(label, kwargs, expected):
    vendors = identify_waap(_resp(**kwargs))
    assert expected in vendors, f"{label}: expected {expected!r} in {vendors!r}"


def test_identify_waap_clean_response_is_empty():
    resp = _resp(
        headers={"server": "nginx", "content-type": "text/html"},
        body="<html><body>hello</body></html>",
    )
    assert identify_waap(resp) == []


def test_identify_waap_returns_list():
    assert isinstance(identify_waap(_resp(headers={"cf-ray": "x"})), list)


# --------------------------------------------------------------------------- #
# cookie_is_valid — Akamai _abck solved-state rule + presence==valid default
# --------------------------------------------------------------------------- #

def test_abck_unsolved_is_invalid():
    # A fresh / unsolved _abck carries the '~-1~' marker.
    assert cookie_is_valid("_abck", "7A2B~-1~XYZ") is False


def test_abck_fresh_both_markers_is_invalid():
    # Newly minted _abck has both '~0~' and '~-1~'; the '~-1~' wins -> not solved.
    assert cookie_is_valid("_abck", "def456~0~-1~-1") is False


def test_abck_solved_is_valid():
    # Solved _abck contains '~0~' and NO '~-1~'.
    assert cookie_is_valid("_abck", "7A2B~0~someblob") is True


def test_cf_clearance_presence_is_valid():
    assert cookie_is_valid("cf_clearance", "x") is True


@pytest.mark.parametrize(
    "name,value",
    [
        ("datadome", "anyvalue"),
        ("waap_id", "anyvalue"),
        ("rbzid", "anyvalue"),
        ("visid_incap_1", "anyvalue"),
        ("aws-waf-token", "anyvalue"),
    ],
)
def test_other_clearance_cookies_presence_is_valid(name, value):
    assert cookie_is_valid(name, value) is True


# --------------------------------------------------------------------------- #
# fingerprint — structured dict with tier / strategy per vendor
# --------------------------------------------------------------------------- #

def test_fingerprint_shape_and_vendor_metadata():
    resp = _resp(
        headers={"cf-ray": "8a1b2c3d-TLV", "server": "cloudflare"},
        set_cookies=["cf_clearance=abc; Path=/"],
    )
    fp = fingerprint(resp)

    assert set(fp.keys()) >= {"url", "status", "vendors"}
    assert fp["status"] == 200
    assert isinstance(fp["vendors"], list) and fp["vendors"], "expected vendors"

    cf = next(v for v in fp["vendors"] if v["name"] == "Cloudflare")
    assert isinstance(cf["tier"], int) and 1 <= cf["tier"] <= 3
    assert isinstance(cf["strategy"], str) and cf["strategy"]
    assert isinstance(cf["evidence"], list) and cf["evidence"]
    assert "cf_clearance" in cf["clearance_cookies"]


def test_fingerprint_clean_response_has_no_vendors():
    fp = fingerprint(_resp(headers={"server": "nginx"}))
    assert fp["vendors"] == []


# --------------------------------------------------------------------------- #
# CLEARANCE_COOKIES export
# --------------------------------------------------------------------------- #

def test_clearance_cookies_export_is_consistent():
    assert isinstance(CLEARANCE_COOKIES, dict)
    # Cookie-clearable vendors are present; Akamai's _abck is among them.
    assert "Akamai" in CLEARANCE_COOKIES
    assert "_abck" in CLEARANCE_COOKIES["Akamai"]
    assert "Cloudflare" in CLEARANCE_COOKIES
    assert "cf_clearance" in CLEARANCE_COOKIES["Cloudflare"]
