#!/usr/bin/env python3
"""score_check.py — read this browser's reCAPTCHA-v3 reputation score.

WHAT THIS TELLS YOU
===================
reCAPTCHA-v3 has no checkbox and no solver. It silently scores the visitor
0.0 (bot) .. 0.9 (human) from reputation: Google account cookies, aged browsing
history, and IP. This script drives a public v3 test page in a Wraith stealth
context and prints the score, so you can self-assess an engine/identity combo.

WHAT TO EXPECT
=============
  * A FRESH automated profile (no Google cookies) scores ~0.1-0.3 no matter how
    clean the fingerprint or which engine you use. That's not a stealth failure
    — it's the absence of reputation. There is nothing to "fix" in the browser.
  * A WARMED identity scores ~0.9. The way to get there is identity borrowing
    (see borrow_session.py), not better evasion. Pass --borrow-google to inject
    your real google.com cookies first and watch the score jump.

THE PARSING TRAP (handled in wraith.detect.recaptcha_v3_score)
==============================================================
The score test page is a landmine for a naive scraper: its FAQ text literally
contains "0.9", and it caches a STALE "Last Score". recaptcha_v3_score() parses
ONLY the live `Result: <score> | Time: <ts> | Hostname: <host>` line and checks
the timestamp is fresh — never a guessed float.

Run:
    uv run python examples/score_check.py                  # Camoufox, fresh
    uv run python examples/score_check.py --engine chromium
    uv run python examples/score_check.py --borrow-google  # inject real reputation
    uv run python examples/score_check.py --headed --bot-detector
"""
from __future__ import annotations

import argparse
import sys

from wraith import (
    browser,
    recaptcha_v3_score,
    bot_detector,
    find_zen_profiles,
    find_firefox_profiles,
    extract_cookies,
    inject_cookies,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Launch a Wraith stealth browser and print the live "
        "reCAPTCHA-v3 reputation score.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--engine", choices=["auto", "camoufox", "chromium"],
                   default="camoufox", help="camoufox (Firefox) is primary.")
    p.add_argument("--headed", action="store_true",
                   help="visible window (headless Camoufox usually scores HIGHER).")
    p.add_argument("--borrow-google", action="store_true",
                   help="inject your real google.com cookies from a Firefox/Zen "
                        "profile first, to demonstrate the reputation jump.")
    p.add_argument("--bot-detector", action="store_true",
                   help="also run the rebrowser bot-detector checks.")
    args = p.parse_args(argv)

    with browser(engine=args.engine, headless=not args.headed, geoip=True) as s:
        if args.borrow_google:
            profiles = find_zen_profiles() or find_firefox_profiles()
            if not profiles:
                print("no Firefox/Zen profile to borrow reputation from",
                      file=sys.stderr)
                return 1
            cookies = extract_cookies(profiles[0], domain_filter="google.com")
            n = inject_cookies(s.context, cookies)
            print(f"[*] injected {n} google.com cookies from {profiles[0].name} "
                  f"(borrowed reputation)")
        else:
            print("[*] scoring a fresh (cold) profile — expect ~0.1-0.3")

        score = recaptcha_v3_score(s.page)
        band = ("human-range" if score >= 0.7
                else "ambiguous" if score >= 0.5 else "bot-range")
        print(f"\nreCAPTCHA-v3 score: {score}  ({band})")

        if args.bot_detector:
            print("\nrebrowser bot-detector:")
            for test, status in bot_detector(s.page).items():
                print(f"  {test:26} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
