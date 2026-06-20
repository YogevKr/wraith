#!/usr/bin/env python3
"""borrow_session.py — Identity borrowing: be a warmed, trusted user, not a fresh bot.

THE SIGNATURE WRAITH WORKFLOW
=============================
Reputation defenses (reCAPTCHA-v3, Reblaze/Link11 ac_v2, Akamai) score *who you
are*, not *how clean your fingerprint is*. A fresh automated profile scores like
a bot no matter how good the stealth — reCAPTCHA-v3 in particular is a pure
reputation score (Google account cookies + aged history + IP, 0.0..1.0) with NO
solver: a fresh profile sits at ~0.1-0.3, a real warmed browser at ~0.9. You
cannot fake it.

So we don't fight it. We BORROW a warmed identity:

  1. read the user's live session cookies straight off their real Firefox/Zen
     profile on disk (they already logged in, already passed any reCAPTCHA gate),
  2. inject those cookies into a Camoufox (Firefox-engine) stealth context,
  3. navigate to the target — we arrive already authenticated, skipping the
     login / reCAPTCHA flow entirely,
  4. harvest the resulting session (cookies + any Authorization bearer the app
     mints) to a reusable file so later runs need no profile access at all.

Why Camoufox (Firefox) and not Chromium here? The reCAPTCHA/Reblaze challenge
JS has an isChrome() branch; under Firefox it's false, so the whole
Chrome-specific detection cluster (window.chrome===undefined-while-UA-says-Chrome,
HeadlessChrome UA sniff, $cdc_/__*driver_* leaks) is simply never run. Camoufox
is Wraith's PRIMARY engine; patched-Chromium (patchright) is the FALLBACK.

Run:
    python borrow_session.py --url https://www.elal.com/heb/ \
        --cookie-domain elal.com \
        --api-host booking.elal.com \
        --out elal.session.json

    # point at a specific Firefox/Zen profile instead of autodetecting:
    python borrow_session.py --url ... --profile "~/Library/.../Profiles/xxxx.default"

Nothing here requires a network or a logged-in profile to *read* — every step is
commented so the flow is clear before you install deps or run it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# wraith.* is the toolkit's public API. Imports are kept inside main() further
# below as well, but we surface them here so the dependency surface is obvious:
#
#   wraith.identity — read cookies off real browser profiles on disk, map them
#                     to Playwright's add_cookies() shape, and inject them.
#   wraith.engine   — launch the stealth browser (Camoufox primary). Returns a
#                     normal Playwright BrowserContext, fully configured.
#   wraith.harvest  — sniff live network traffic for the auth bearer + cookie
#                     and persist a reusable {Authorization, Cookie, User-Agent}
#                     session file.
from wraith import engine, harvest, identity


async def borrow(args: argparse.Namespace) -> int:
    # --- 1. Locate the real browser profile -------------------------------
    # identity.find_firefox_profiles() walks the standard OS locations for
    # Firefox AND Zen (Zen is a Firefox fork, same on-disk layout):
    #   macOS:  ~/Library/Application Support/{Firefox,zen}/Profiles/*/
    #   Linux:  ~/.mozilla/firefox/*/  and  ~/.zen/*/
    #   Win:    %APPDATA%/{Mozilla/Firefox,zen}/Profiles/*/
    # Each profile dir contains cookies.sqlite (the prize).
    if args.profile:
        profile = identity.FirefoxProfile(Path(args.profile).expanduser())
    else:
        profiles = identity.find_firefox_profiles()
        if not profiles:
            print(
                "No Firefox/Zen profile found. Pass --profile to point at one "
                "explicitly (the dir containing cookies.sqlite).",
                file=sys.stderr,
            )
            return 2
        # Prefer the most-recently-used profile — that's the one the human is
        # actually logged into.
        profile = identity.pick_default_profile(profiles)
    print(f"[*] borrowing identity from profile: {profile.path}")

    # --- 2. Extract cookies for the target domain -------------------------
    # IMPORTANT GOTCHA: cookies.sqlite is locked (and may have unflushed -wal
    # data) while the browser is running. extract_cookies() copies the .sqlite
    # PLUS its -wal/-shm sidecars to a temp dir first, then reads the copy, so
    # you do NOT have to quit the browser.
    #
    # It reads table moz_cookies (host,name,value,path,isSecure,isHttpOnly,
    # sameSite) and normalizes Firefox's integer sameSite into Playwright's
    # string form:  0 -> "None", 1 -> "Lax", 2 -> "Strict".
    # (Note: a "None" cookie is only valid when secure=True; the mapper enforces
    # that.) The returned objects are already in context.add_cookies() shape.
    cookies = identity.extract_cookies(profile, domain=args.cookie_domain)
    if not cookies:
        print(
            f"[!] no cookies found for domain '{args.cookie_domain}'. Are you "
            f"logged in to that site in this profile?",
            file=sys.stderr,
        )
        return 3
    print(f"[*] extracted {len(cookies)} cookies for '{args.cookie_domain}'")

    # --- 3. Launch the stealth engine -------------------------------------
    # engine.launch_context() returns an async context manager yielding a
    # Playwright BrowserContext. Defaults are the battle-tested ones:
    #   engine="camoufox"  -> Firefox stealth, the engine that beats
    #                          reCAPTCHA-v3 + Reblaze (see module docstring).
    #   geoip=True         -> derive timezone + locale from the EXIT IP so the
    #                          identity is internally consistent. Mismatched
    #                          tz/locale vs IP is itself a tell.
    #   headless=True      -> for Camoufox, HEADLESS actually scores *higher*
    #                          than headed on browser benchmarks.
    #
    # Camoufox 0.4.x has a hard incompatibility with playwright>=1.60 (a Firefox
    # pageError serialization crash in coreBundle.js). engine.launch_context()
    # asserts playwright==1.55.x for the camoufox path and raises a clear error
    # otherwise — so you find out at launch, not mid-navigation.
    async with engine.launch_context(
        engine="camoufox",
        geoip=True,
        headless=True,
        locale=args.locale,  # None -> let geoip decide
    ) as context:
        # --- 4. Inject the borrowed cookies -------------------------------
        # Thin wrapper over context.add_cookies(cookies). We do it on the
        # context (not a page) so every tab inherits the borrowed identity.
        await identity.inject_cookies(context, cookies)
        print("[*] injected borrowed cookies into stealth context")

        # --- 5. Start harvesting BEFORE we navigate -----------------------
        # Many auth bearers are NOT cookies — they're minted per-session and
        # sent as an Authorization header on XHR/fetch to the API host. The
        # harvester listens on context.on('request') and captures the FIRST
        # request to api_host that carries Authorization, recording its
        # Authorization + Cookie + User-Agent. Attaching before navigation
        # means we don't miss the early bootstrap calls.
        session_harvester = harvest.SessionHarvester(context, api_host=args.api_host)
        session_harvester.start()

        # --- 6. Navigate as the already-authenticated user ----------------
        page = await context.new_page()
        # A little synthetic mouse entropy helps reputation scores; an instant
        # robotic interaction hurts them. We're only navigating here, but the
        # helper is the right place for human-like motion if you add clicks.
        await engine.human_goto(page, args.url)
        print(f"[*] navigated to {args.url} as the borrowed user")

        # Give the page's bootstrap XHRs a moment to fire so the harvester can
        # see the Authorization-bearing request.
        await page.wait_for_timeout(args.settle_ms)

        # --- 7. Persist the harvested session -----------------------------
        # Writes {Authorization, Cookie, User-Agent} (whatever was captured) so
        # subsequent runs can hit the API directly with httpx/requests and never
        # need to touch the user's profile again.
        session = session_harvester.snapshot()
        out_path = Path(args.out).expanduser()
        out_path.write_text(json.dumps(session.to_dict(), indent=2))
        print(f"[*] harvested session -> {out_path}")
        if not session.authorization:
            print(
                "[i] note: no Authorization bearer was seen. The site may be "
                "cookie-only auth (the cookies in the file are enough), or the "
                "bearer fires on a route you didn't visit — try --url deeper "
                "into the authed area, or raise --settle-ms.",
                file=sys.stderr,
            )

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Borrow a warmed, authenticated identity from a real "
        "Firefox/Zen profile and drive a target as that user.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--url",
        required=True,
        help="Target URL to open as the authenticated user.",
    )
    p.add_argument(
        "--cookie-domain",
        required=True,
        help="Registrable domain whose cookies to borrow, e.g. 'elal.com'. "
        "Matches that host and its subdomains.",
    )
    p.add_argument(
        "--api-host",
        default=None,
        help="API host to watch for an Authorization bearer, e.g. "
        "'booking.elal.com'. Defaults to the --url host.",
    )
    p.add_argument(
        "--profile",
        default=None,
        help="Path to a specific Firefox/Zen profile dir (the one containing "
        "cookies.sqlite). Default: autodetect the most-recent profile.",
    )
    p.add_argument(
        "--locale",
        default=None,
        help="Force a locale, e.g. 'he-IL'. Default: derived from exit IP via "
        "geoip so it stays consistent with the IP's region.",
    )
    p.add_argument(
        "--settle-ms",
        type=int,
        default=8000,
        help="How long to wait after navigation for bootstrap XHRs (and thus "
        "the Authorization bearer) to fire.",
    )
    p.add_argument(
        "--out",
        default="session.json",
        help="Where to write the harvested reusable session file.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.api_host is None:
        # Default the API host to the navigation host.
        from urllib.parse import urlparse

        args.api_host = urlparse(args.url).hostname
    return asyncio.run(borrow(args))


if __name__ == "__main__":
    raise SystemExit(main())
