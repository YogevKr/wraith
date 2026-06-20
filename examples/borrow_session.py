#!/usr/bin/env python3
"""borrow_session.py — Identity borrowing: be a warmed, trusted user, not a fresh bot.

THE SIGNATURE WRAITH WORKFLOW
=============================
Reputation defenses (reCAPTCHA-v3, Reblaze/Link11 ac_v2, Akamai) score *who you
are*, not *how clean your fingerprint is*. A fresh automated profile scores like
a bot no matter how good the stealth — reCAPTCHA-v3 is a pure reputation score
(Google account cookies + aged history + IP, 0.0..0.9) with NO solver: a fresh
profile sits at ~0.1-0.3, a real warmed browser at ~0.9. You cannot fake it.

So we don't fight it. We BORROW a warmed identity:
  1. read the user's live session cookies straight off their real Firefox/Zen
     profile on disk (they already logged in, already passed any reCAPTCHA gate),
  2. inject those cookies into a Camoufox (Firefox-engine) stealth context,
  3. navigate to the target — we arrive already authenticated, skipping the
     login / reCAPTCHA flow entirely,
  4. harvest the resulting session (cookies + any Authorization bearer the app
     mints) to a reusable file so later runs need no profile access at all.

Why Camoufox (Firefox) and not Chromium? The reCAPTCHA/Reblaze challenge JS has
an isChrome() branch; under Firefox it's false, so the whole Chrome-specific
detection cluster (window.chrome===undefined-while-UA-says-Chrome, HeadlessChrome
UA sniff, $cdc_/__*driver_* leaks) is never run. Camoufox is Wraith's PRIMARY
engine; patched-Chromium (patchright) is the FALLBACK.

Run:
    uv run python examples/borrow_session.py \\
        --url https://booking.elal.com/booking/flights \\
        --cookie-domain elal.com \\
        --api-host booking.elal.com \\
        --out elal.session.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

from wraith import (
    browser,
    find_zen_profiles,
    find_firefox_profiles,
    extract_cookies,
    inject_cookies,
    SessionHarvester,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Borrow a warmed, authenticated identity from a real "
        "Firefox/Zen profile and harvest a reusable API session.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--url", required=True,
                   help="target URL to open as the authenticated user.")
    p.add_argument("--cookie-domain", required=True,
                   help="domain whose cookies to borrow, e.g. 'elal.com' "
                        "(matches host + subdomains).")
    p.add_argument("--api-host", default=None,
                   help="API host to watch for an Authorization bearer "
                        "(default: the --url host).")
    p.add_argument("--profile", default=None,
                   help="explicit Firefox/Zen profile dir (default: autodetect).")
    p.add_argument("--auth-cookie", default=None,
                   help="optional cookie name the captured request must carry "
                        "(e.g. SMSESSION) to confirm it's the authed call.")
    p.add_argument("--locale", default=None,
                   help="force a locale, e.g. he-IL (default: geoip-derived).")
    p.add_argument("--settle-ms", type=int, default=8000,
                   help="wait after navigation for bootstrap XHRs to fire.")
    p.add_argument("--out", default="session.json",
                   help="where to write the harvested session file.")
    args = p.parse_args(argv)

    api_host = args.api_host or urlparse(args.url).hostname

    # 1. locate the real browser profile (Zen is a Firefox fork, same layout).
    if args.profile:
        profiles = [Path(args.profile).expanduser()]
    else:
        profiles = find_zen_profiles() + find_firefox_profiles()
    if not profiles:
        print("No Firefox/Zen profile found; pass --profile.", file=sys.stderr)
        return 2
    profile = profiles[0]
    print(f"[*] borrowing identity from: {profile}")

    # 2. extract cookies for the target domain. extract_cookies() copies the
    #    locked cookies.sqlite (+ -wal) to temp first, so the browser can stay open.
    cookies = extract_cookies(profile, domain_filter=args.cookie_domain)
    if not cookies:
        print(f"[!] no '{args.cookie_domain}' cookies — are you logged in there "
              f"in this profile?", file=sys.stderr)
        return 3
    print(f"[*] extracted {len(cookies)} cookies for '{args.cookie_domain}'")

    # 3. launch the stealth engine (Camoufox; geoip keeps tz/locale consistent).
    with browser(engine="camoufox", headless=True, geoip=True,
                 locale=args.locale) as s:
        # 4. inject the borrowed cookies onto the context (all tabs inherit them).
        inject_cookies(s.context, cookies)
        print("[*] injected borrowed cookies into the stealth context")

        # 5. attach the harvester BEFORE navigating so early bootstrap XHRs (which
        #    carry the minted Authorization bearer) are not missed.
        harvester = SessionHarvester(
            target_host=api_host, auth_cookie=args.auth_cookie,
        ).attach(s.context)

        # 6. navigate as the already-authenticated user.
        s.page.goto(args.url)
        print(f"[*] navigated to {args.url} as the borrowed user")
        s.page.wait_for_timeout(args.settle_ms)

        # 7. persist {Authorization, Cookie, User-Agent} for reuse with httpx etc.
        session = harvester.save_session(args.out)
        print(f"[*] harvested session -> {args.out}")
        if not session.get("headers", {}).get("Authorization"):
            print("[i] no Authorization bearer captured — site may be cookie-only "
                  "auth, or the bearer fires deeper in; try a deeper --url or a "
                  "larger --settle-ms.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
