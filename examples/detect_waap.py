#!/usr/bin/env python3
"""detect_waap.py — Given a URL, print which bot / WAAP systems guard it.

WHY FINGERPRINT FIRST
=====================
Knowing the defense decides the strategy. Wraith reads the signal from response
headers, cookies, status codes, and in-page JS globals, then names the stack:

  Reblaze / Link11   server 'rhino-core-shield'; cookies 'rbzid'/'waap_id';
                     status 247 (challenge) / 248 (token) / 492 (hard block, e.g.
                     a 'HeadlessChrome' UA); challenge sets window.rbzns =
                     {seed, bereshit:'1'} + winsocks(). ac_v2 = one-shot
                     fingerprint + seed-keyed SHA-1 hashcash, NO behavioral track.
                     >> No public bypass exists — even commercial multi-WAF SDKs
                        skip it. The win is engine choice (Firefox/Camoufox dodges
                        the isChrome() cluster) + identity borrowing, not a solver.
  Akamai             cookies '_abck'/'bm_sz'/'AKA_A2'; header 'x-akamai-transformed'.
  reCAPTCHA          grecaptcha globals (v3 is reputation-scored — see score_check.py).
  DataDome           cookie 'datadome'.
  Incapsula/Imperva  cookies 'visid_incap'/'reese84'.
  SiteMinder         cookie 'SMSESSION'; redirects through '/siteminderagent/'.

EL AL, for instance, layers Reblaze/Link11 + reCAPTCHA-v3 + Akamai + SiteMinder.

Run:
    uv run python examples/detect_waap.py https://www.elal.com/heb/
    uv run python examples/detect_waap.py https://example.com
    uv run python examples/detect_waap.py https://www.elal.com/heb/ --browser
"""
from __future__ import annotations

import argparse
import sys

from wraith import identify_waap


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Fingerprint the bot/WAAP systems protecting a URL.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("url", help="target URL to fingerprint.")
    p.add_argument("--browser", action="store_true",
                   help="fetch with a stealth browser (catches JS-only signals "
                        "like window.rbzns/grecaptcha) instead of a plain httpx probe.")
    p.add_argument("--engine", choices=["auto", "camoufox", "chromium"],
                   default="camoufox")
    args = p.parse_args(argv)

    if args.browser:
        # Drive a real stealth context so post-execution globals are visible.
        from wraith import browser
        with browser(engine=args.engine, headless=True, geoip=True) as s:
            s.page.goto(args.url)
            s.page.wait_for_timeout(4000)
            systems = identify_waap(s.page)
    else:
        # Plain header/cookie/status probe over httpx — fast, no browser needed.
        systems = identify_waap(args.url)

    print(f"URL: {args.url}")
    if not systems:
        print("\nNo known bot/WAAP systems fingerprinted.")
        print("(A passive defense may only engage on suspicious traffic; try "
              "--browser, or probe from a flagged IP.)")
        return 0

    print(f"\nDetected {len(systems)} protection system(s):")
    for name in systems:
        print(f"  • {name}")

    low = " ".join(systems).lower()
    if "reblaze" in low or "link11" in low:
        print("\nStrategy: Reblaze/Link11 has no public bypass. Use the Camoufox "
              "(Firefox) engine to dodge its isChrome() cluster, and borrow a "
              "warmed session (borrow_session.py) — don't try to solve it.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
