#!/usr/bin/env python3
"""score_check.py — Launch the stealth browser and read your reCAPTCHA-v3 score.

WHAT THIS TELLS YOU
===================
reCAPTCHA-v3 has no checkbox and no solver. It silently scores the visitor
0.0 (bot) .. 1.0 (human) from reputation: Google account cookies, aged browsing
history, and IP. This script drives a public v3 test page in a Wraith stealth
context and prints the score, so you can self-assess an engine/identity combo.

WHAT TO EXPECT
=============
  * A FRESH automated profile (no Google cookies) scores ~0.1-0.3 no matter how
    clean the fingerprint or which engine you use. That's not a stealth failure
    — it's the absence of reputation. There is nothing to "fix" in the browser.
  * A WARMED identity scores ~0.9. The way to get there is identity borrowing,
    not better evasion — see borrow_session.py. Run that first, then this with
    --reuse-session to score the borrowed identity.
So a low number here is the expected baseline and is precisely why Wraith's
strategy is to borrow reputation rather than manufacture it.

THE PARSING TRAP (this bit us — do not "fix" it)
================================================
The score test pages are landmines for a naive scraper:
  * the page's own FAQ text literally contains "0.9" as an example, and
  * it caches and re-displays a STALE "Last Score" from a previous visitor.
Regexing the page for a float will happily return one of those and lie to you.
wraith.detect.read_recaptcha_v3_score() instead parses ONLY the live result
line of the form:
    Result: <score> | Time: <timestamp> | Hostname: <host>
and verifies the timestamp is FRESH (within a tolerance) before trusting the
number. If the line is missing or stale, it raises rather than returning a
fabricated score.

Run:
    python score_check.py                       # default test page, fresh profile
    python score_check.py --engine patchright   # score the Chromium fallback
    python score_check.py --reuse-session session.json   # score a borrowed identity
    python score_check.py --headed              # watch it (Camoufox: headless scores higher!)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# detect: diagnostics/parsers (reCAPTCHA score, bot-detector, WAAP id).
# engine: the stealth browser launcher.
# identity: only needed when scoring a previously-borrowed session.
from wraith import detect, engine, identity

# A well-known public reCAPTCHA-v3 score harness. cleantalk's page renders the
# canonical 'Result: X | Time: ... | Hostname: ...' line the parser keys on.
DEFAULT_SCORE_URL = "https://cleantalk.org/recaptcha-v3-score-test"


async def check(args: argparse.Namespace) -> int:
    # Camoufox is primary and, importantly for THIS measurement, headless
    # Camoufox tends to score higher than headed — so headless is the default.
    # patchright (patched Chromium) is the fallback engine; it suppresses the
    # Runtime.enable CDP leak. If you switch to Chromium stealth elsewhere,
    # engine.launch_context() applies the required hardening:
    #   viewport=None (Playwright's default 1280x720 viewport is a RED flag),
    #   ignore_default_args ['--enable-automation','--enable-unsafe-swiftshader'].
    async with engine.launch_context(
        engine=args.engine,
        geoip=True,            # tz+locale from exit IP -> consistent identity
        headless=not args.headed,
    ) as context:
        # Optionally score a BORROWED identity instead of a cold one. This is the
        # interesting case: it demonstrates the jump from bot-range to human-range
        # once real reputation cookies are injected.
        if args.reuse_session:
            sess = Path(args.reuse_session).expanduser()
            # Re-inject the cookies captured by borrow_session.py. The score will
            # reflect whatever Google reputation rode along in those cookies.
            cookies = identity.cookies_from_session_file(sess)
            await identity.inject_cookies(context, cookies)
            print(f"[*] scoring borrowed identity from {sess}")
        else:
            print("[*] scoring a fresh (cold) profile — expect ~0.1-0.3")

        page = await context.new_page()
        await engine.human_goto(page, args.url)

        # read_recaptcha_v3_score() runs the page's v3 execution, then parses ONLY
        # the live 'Result: ... | Time: ... | Hostname: ...' line and checks the
        # timestamp freshness. It returns a small result object so you also get
        # the hostname the token was minted for (useful when a page proxies the
        # check). Raises ValueError if no fresh line is found — never guesses.
        try:
            result = await detect.read_recaptcha_v3_score(
                page,
                max_age_seconds=args.max_age,
            )
        except ValueError as e:
            print(f"[!] could not read a fresh score: {e}", file=sys.stderr)
            print(
                "    (the page may have only shown a cached 'Last Score', or the "
                "    token didn't execute — try --headed or raise --max-age)",
                file=sys.stderr,
            )
            return 1

        # Pretty-print. The score is the headline; band it for quick reading.
        band = (
            "human-range" if result.score >= 0.7
            else "ambiguous" if result.score >= 0.5
            else "bot-range"
        )
        print(
            json.dumps(
                {
                    "engine": args.engine,
                    "borrowed_identity": bool(args.reuse_session),
                    "score": result.score,
                    "band": band,
                    "hostname": result.hostname,
                    "scored_at": result.timestamp.isoformat(),
                },
                indent=2,
            )
        )
        # Headline line for humans / log scraping.
        print(f"\nreCAPTCHA-v3 score: {result.score}  ({band})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Launch a Wraith stealth browser and print the live "
        "reCAPTCHA-v3 reputation score.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--url",
        default=DEFAULT_SCORE_URL,
        help="reCAPTCHA-v3 score test page.",
    )
    p.add_argument(
        "--engine",
        choices=["camoufox", "patchright"],
        default="camoufox",
        help="Stealth engine. camoufox (Firefox) is primary; patchright "
        "(patched Chromium) is the fallback.",
    )
    p.add_argument(
        "--reuse-session",
        default=None,
        metavar="SESSION_JSON",
        help="Score a borrowed identity: inject cookies from a session file "
        "produced by borrow_session.py (expect a much higher score).",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible window. NOTE: headless Camoufox usually "
        "scores HIGHER, so headless is the default.",
    )
    p.add_argument(
        "--max-age",
        type=int,
        default=30,
        help="Reject the parsed score if its 'Time:' stamp is older than this "
        "many seconds (guards against the page's cached stale 'Last Score').",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(check(args))


if __name__ == "__main__":
    raise SystemExit(main())
