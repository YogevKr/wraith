#!/usr/bin/env python3
"""detect_waap.py — Given a URL, print which bot / WAAP systems guard it.

WHY FINGERPRINT FIRST
=====================
Knowing the defense decides the strategy. Wraith reads the signal from response
headers, cookies, status codes, and in-page JS globals, then names the stack so
you can choose an approach:

  Reblaze / Link11   server: 'rhino-core-shield'; cookies 'rbzid' / 'waap_id';
                     status 247 (challenge) / 248 (token) / 492 (hard block —
                     e.g. a UA containing 'HeadlessChrome'); the challenge page
                     sets window.rbzns = {seed, bereshit:'1'} and calls
                     winsocks(). Its 'ac_v2' check is a ONE-SHOT fingerprint +
                     seed-keyed SHA-1 hashcash with NO behavioral tracking.
                     >> No public bypass exists for Reblaze/Link11 — even
                        commercial multi-WAF SDKs skip it. The win is engine
                        choice (Firefox/Camoufox dodges the isChrome() cluster)
                        plus identity borrowing, not a "solver".
  Akamai             cookies '_abck' / 'bm_sz' / 'AKA_A2'; header
                     'x-akamai-transformed'.
  reCAPTCHA          grecaptcha globals in-page (v3 is reputation-scored — see
                     score_check.py; there is no solver).
  DataDome           cookie 'datadome'.
  Incapsula/Imperva  cookies 'visid_incap' / 'reese84'.
  SiteMinder         cookie 'SMSESSION'; redirects through '/siteminderagent/'.

EL AL, for instance, layers Reblaze/Link11 + reCAPTCHA-v3 + Akamai + SiteMinder
— this tool surfaces all of them at once.

HOW IT LOOKS
===========
We fetch with a real Wraith stealth context (not bare httpx) so the response is
what a browser actually receives — some WAAPs only reveal themselves to a full
browser, and a challenge page's JS globals (rbzns, grecaptcha) are only visible
after the page executes.

Run:
    python detect_waap.py https://www.elal.com/heb/
    python detect_waap.py https://example.com --json
    python detect_waap.py https://example.com --engine patchright
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

# detect: the WAAP fingerprinter. engine: the stealth browser so we observe what
# a real browser sees (headers + status + post-execution JS globals).
from wraith import detect, engine


async def run(args: argparse.Namespace) -> int:
    async with engine.launch_context(
        engine=args.engine,
        geoip=True,
        headless=not args.headed,
    ) as context:
        page = await context.new_page()

        # detect.identify_waap() drives the navigation itself so it can collect
        # the FULL signal set:
        #   * the main response's status + headers (server, x-akamai-transformed,
        #     set-cookie),
        #   * cookies present in the context after load (rbzid, _abck, datadome,
        #     visid_incap, SMSESSION, ...),
        #   * in-page JS globals that only exist post-execution (window.rbzns,
        #     window.grecaptcha, winsocks).
        # It returns a report listing each detected system with the evidence that
        # triggered it, plus the raw HTTP status (e.g. a Reblaze 247/492 is itself
        # a strong signal).
        report = await detect.identify_waap(page, args.url)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0

    # --- human-readable output -------------------------------------------
    print(f"URL:    {args.url}")
    print(f"Status: {report.http_status}")
    if not report.systems:
        print("\nNo known bot/WAAP systems detected.")
        print(
            "(That doesn't prove there's none — a passive defense may only "
            "engage on suspicious traffic. Re-run from a flagged IP to be sure.)"
        )
        return 0

    print(f"\nDetected {len(report.systems)} protection system(s):\n")
    for sys_hit in report.systems:
        print(f"  • {sys_hit.name}")
        # Each hit carries the concrete evidence (which header/cookie/global)
        # so the result is auditable, not a black-box guess.
        for ev in sys_hit.evidence:
            print(f"      - {ev}")
        if sys_hit.note:
            print(f"      ! {sys_hit.note}")
        print()

    # Reblaze/Link11 gets a loud strategy hint because it has no public bypass.
    if any(s.name.lower().startswith("reblaze") for s in report.systems) or any(
        "link11" in s.name.lower() for s in report.systems
    ):
        print(
            "Strategy: Reblaze/Link11 has no public bypass. Use the Camoufox "
            "(Firefox) engine to dodge its isChrome() detection cluster, and "
            "borrow a warmed session (see borrow_session.py) rather than trying "
            "to solve the challenge.",
            file=sys.stderr,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fingerprint the bot/WAAP systems protecting a URL.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("url", help="Target URL to fingerprint.")
    p.add_argument(
        "--engine",
        choices=["camoufox", "patchright"],
        default="camoufox",
        help="Stealth engine used to fetch (so we see what a real browser sees).",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible window (default headless).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the full report as JSON instead of human-readable text.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
