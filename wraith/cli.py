"""Wraith command-line interface.

Subcommands:

* ``launch``  — open a stealth browser at a URL (Camoufox primary, patchright
  fallback).
* ``borrow``  — extract cookies from a real on-disk browser profile, inject
  them, and open the target as the already-authenticated user.
* ``harvest`` — open the target and capture a live authenticated session
  (Authorization + auth cookie) to a JSON file.
* ``score``   — run a reCAPTCHA-v3 reputation score check.
* ``detect``  — identify the WAAP / anti-bot stack guarding a URL.
* ``agent``   — open an agent browser to a URL and print the indexed snapshot.
* ``mcp``     — run the MCP server (stdio) exposing the agent over tools.

Design notes
------------
Every heavy module (:mod:`wraith.engine`, :mod:`wraith.identity`,
:mod:`wraith.harvest`, :mod:`wraith.detect`) is imported *lazily inside the
command handler that needs it*. That keeps ``wraith --help`` — and the whole
argument parser — working on a partial install where some of those modules (or
their browser dependencies) are missing. A missing module surfaces as a clean
error message + non-zero exit, never an import traceback at startup.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from . import __version__

# Default cookie names worth auto-trying when the user does not name one.
# These are the session/auth cookies of the WAAPs in our threat model.
_KNOWN_AUTH_COOKIES = ("rbzid", "waap_id", "SMSESSION", "datadome", "_abck")

_DEFAULT_SCORE_URL = "https://cleantalk.org/recaptcha-v3-score-test"


# --------------------------------------------------------------------------
# lazy-import helpers
# --------------------------------------------------------------------------

def _lazy(module: str):
    """Import ``wraith.<module>`` lazily, raising a friendly CLI error.

    We raise :class:`SystemExit` so the caller's traceback stays clean and the
    process exits non-zero with an actionable message.
    """
    import importlib

    try:
        return importlib.import_module(f"wraith.{module}")
    except Exception as exc:  # ImportError or a dependency error inside it
        raise SystemExit(
            f"wraith: the '{module}' component is not available "
            f"({type(exc).__name__}: {exc}).\n"
            f"       Install the full toolkit, e.g. `uv sync` / "
            f"`pip install 'wraith[all]'`."
        )


# --------------------------------------------------------------------------
# command handlers
# --------------------------------------------------------------------------

def cmd_launch(args: argparse.Namespace) -> int:
    """Open a stealth browser at a URL and keep it open."""
    engine = _lazy("engine")
    print(
        f"wraith: launching {args.engine} engine "
        f"({'headless' if args.headless else 'headed'}) -> {args.url}",
        file=sys.stderr,
    )
    with engine.launch(
        args.engine,
        headless=args.headless,
        geoip=not args.no_geoip,
        proxy=args.proxy,
    ) as session:
        page = _page_of(session)
        page.goto(args.url, wait_until="domcontentloaded")
        print(f"wraith: opened {page.url}", file=sys.stderr)
        if not args.headless and not args.no_wait:
            _hold_open()
    return 0


def cmd_borrow(args: argparse.Namespace) -> int:
    """Extract+inject cookies from a real profile, then open the target."""
    engine = _lazy("engine")
    identity = _lazy("identity")

    host = args.host or _host_of(args.url)
    profile_path = _resolve_profile(identity, args.profile)
    print(
        f"wraith: borrowing cookies from {profile_path} for host {host!r}",
        file=sys.stderr,
    )
    cookies = identity.extract_cookies(profile_path, domain_filter=host or None)
    print(f"wraith: extracted {len(cookies)} cookie(s)", file=sys.stderr)

    with engine.launch(
        args.engine,
        headless=args.headless,
        geoip=not args.no_geoip,
        proxy=args.proxy,
    ) as session:
        context = _context_of(session)
        injected = identity.inject_cookies(context, cookies)
        print(f"wraith: injected {injected} cookie(s)", file=sys.stderr)
        page = _page_of(session, context)
        page.goto(args.url, wait_until="domcontentloaded")
        print(f"wraith: opened {page.url} as borrowed identity", file=sys.stderr)
        if not args.headless and not args.no_wait:
            _hold_open()
    return 0


def _resolve_profile(identity, profile: Optional[str]):
    """Turn a ``--profile`` hint (name or path) into a profile directory path.

    Accepts an explicit filesystem path, a browser name
    (firefox/zen/chrome), or ``None`` (auto-detect: first Zen, then Firefox,
    then Chrome). Raises :class:`SystemExit` with guidance if none is found.
    """
    from pathlib import Path

    if profile:
        p = Path(profile).expanduser()
        if p.exists():
            return p
        key = profile.strip().lower()
    else:
        key = None

    def first(seq):
        seq = list(seq or [])
        return seq[0] if seq else None

    if key in (None, "zen"):
        found = first(identity.find_zen_profiles())
        if found:
            return found
    if key in (None, "firefox", "ff"):
        found = first(identity.find_firefox_profiles())
        if found:
            return found
    if key in (None, "chrome", "chromium"):
        found = identity.find_chrome_profile()
        if found:
            return found

    raise SystemExit(
        f"wraith: could not locate a browser profile for "
        f"{profile or 'auto-detect'!r}. Pass --profile with an explicit "
        f"path to the profile directory."
    )


def cmd_harvest(args: argparse.Namespace) -> int:
    """Open the target and capture a live authenticated session to a file."""
    harvest = _lazy("harvest")

    target = args.target or _host_of(args.url)
    if not target:
        print("wraith: --target or a URL is required", file=sys.stderr)
        return 2

    print(
        f"wraith: harvesting session from {target!r} "
        f"(cookie={args.cookie or 'any'}) -> {args.out}",
        file=sys.stderr,
    )
    try:
        payload = harvest.harvest_session(
            target_url=target,
            out_path=args.out,
            url=args.url,
            auth_cookie=args.cookie,
            auth_header=args.header,
            borrow_from=args.borrow,
            engine=args.engine,
            headless=args.headless,
            geoip=not args.no_geoip,
            timeout=args.timeout,
        )
    except RuntimeError as exc:
        print(f"wraith: {exc}", file=sys.stderr)
        return 1

    auth = payload.get("headers", {}).get("Authorization", "")
    print(
        f"wraith: captured session -> {args.out} "
        f"(Authorization: {auth[:16]}... , saved_at {payload.get('saved_at')})",
        file=sys.stderr,
    )
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """Run the reCAPTCHA-v3 reputation score check.

    ``detect.recaptcha_v3_score`` accepts a live ``Page`` *or* a zero-arg
    callable returning one. We hand it a launcher closure so it opens the
    stealth engine itself and tears it down when done.
    """
    detect = _lazy("detect")
    engine = _lazy("engine")
    print(f"wraith: checking reCAPTCHA-v3 score (engine={args.engine})", file=sys.stderr)

    sessions = []

    def launcher():
        session = engine.launch(
            args.engine,
            headless=args.headless,
            geoip=not args.no_geoip,
        )
        sessions.append(session)
        return session.page

    try:
        score = detect.recaptcha_v3_score(launcher)
    except ValueError as exc:
        print(f"wraith: could not read a fresh score: {exc}", file=sys.stderr)
        return 1
    finally:
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass

    if args.json:
        print(json.dumps({"score": score, "url": args.url}, indent=2))
    else:
        verdict = (
            "bot-looking — fall back to identity borrowing"
            if score < 0.5
            else "warmed / trusted"
        )
        print(f"reCAPTCHA-v3 score: {score:.2f}  ({verdict})")
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    """Identify the WAAP / anti-bot stack on a URL."""
    detect = _lazy("detect")
    print(f"wraith: probing {args.url} for WAAP signatures", file=sys.stderr)
    waaps = detect.identify_waap(args.url)

    if args.json:
        print(json.dumps({"url": args.url, "waaps": list(waaps)}, indent=2))
        return 0

    if not waaps:
        print(f"{args.url}: no known WAAP signatures detected")
    else:
        print(f"{args.url}: detected -> {', '.join(map(str, waaps))}")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    """Open an :class:`~wraith.agent.AgentBrowser` to a URL and print the snapshot.

    This drives the full agent perception path — navigate through any WAAP via
    ``clear_challenge``, auto-dismiss cookie/consent banners, then render the
    indexed, browser-use-style snapshot of the page's interactive elements.
    """
    agent = _lazy("agent")
    print(
        f"wraith: opening agent browser ({args.engine}) -> {args.url}",
        file=sys.stderr,
    )
    with agent.agent_browser(
        engine=args.engine,
        headless=args.headless,
        geoip=not args.no_geoip,
    ) as ab:
        snap = ab.navigate(args.url)
        if args.json:
            elements = [
                {
                    "index": e.index,
                    "tag": e.tag,
                    "role": e.role,
                    "text": e.text,
                    "attributes": e.attributes,
                }
                for e in snap.elements
            ]
            print(
                json.dumps(
                    {"url": snap.url, "title": snap.title, "elements": elements},
                    indent=2,
                )
            )
        else:
            print(snap.to_text())
        if not args.headless and not args.no_wait:
            _hold_open()
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Run the Wraith MCP server (stdio transport) for agent integration."""
    mcp = _lazy("mcp")
    print("wraith: starting MCP server (stdio)", file=sys.stderr)
    mcp.main()
    return 0


# --------------------------------------------------------------------------
# small adapters over the integrator modules (kept loose on purpose)
# --------------------------------------------------------------------------

def _context_of(session):
    for attr in ("context", "browser_context"):
        ctx = getattr(session, attr, None)
        if ctx is not None:
            return ctx
    if hasattr(session, "on") and hasattr(session, "new_page"):
        return session
    contexts = getattr(session, "contexts", None)
    if contexts:
        contexts = list(contexts)
        if contexts:
            return contexts[0]
    if hasattr(session, "new_context"):
        return session.new_context()
    return session


def _page_of(session, context=None):
    page = getattr(session, "page", None)
    if page is not None:
        return page
    ctx = context or _context_of(session)
    pages = getattr(ctx, "pages", None)
    if pages:
        pages = list(pages)
        if pages:
            return pages[0]
    return ctx.new_page()


def _host_of(url: Optional[str]) -> str:
    if not url:
        return ""
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _hold_open() -> None:
    print(
        "wraith: browser is open. Press Enter (or Ctrl-C) to close.",
        file=sys.stderr,
    )
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def _add_engine_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--engine",
        choices=["camoufox", "chromium", "auto"],
        default="camoufox",
        help="stealth engine: camoufox (Firefox, primary — beats reCAPTCHA-v3 "
        "/ Reblaze) or chromium (patched-Chromium/patchright backend, "
        "fallback); 'auto' lets the engine choose. default: camoufox",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="run headless (note: Camoufox scores BETTER headless)",
    )
    p.add_argument(
        "--no-geoip",
        action="store_true",
        help="disable geoip-derived timezone/locale matching",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wraith",
        description="The identity-borrowing stealth browser for autonomous "
        "agents. Don't beat reputation defenses — borrow a warmed identity.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"wraith {__version__}"
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # launch
    p_launch = sub.add_parser(
        "launch", help="open a stealth browser at a URL"
    )
    p_launch.add_argument("url", help="URL to open")
    p_launch.add_argument("--proxy", default=None, help="proxy server URL")
    p_launch.add_argument(
        "--no-wait",
        action="store_true",
        help="do not hold the browser open waiting for Enter",
    )
    _add_engine_flags(p_launch)
    p_launch.set_defaults(func=cmd_launch)

    # borrow
    p_borrow = sub.add_parser(
        "borrow",
        help="extract cookies from a real profile, inject them, open target",
    )
    p_borrow.add_argument("url", help="target URL to open as the borrowed user")
    p_borrow.add_argument(
        "--profile",
        default=None,
        help="source browser/profile (e.g. firefox, zen, chrome). "
        "default: auto-detect",
    )
    p_borrow.add_argument(
        "--host",
        default=None,
        help="cookie host filter (default: derived from URL)",
    )
    p_borrow.add_argument("--proxy", default=None, help="proxy server URL")
    p_borrow.add_argument(
        "--no-wait",
        action="store_true",
        help="do not hold the browser open waiting for Enter",
    )
    _add_engine_flags(p_borrow)
    p_borrow.set_defaults(func=cmd_borrow)

    # harvest
    p_harvest = sub.add_parser(
        "harvest",
        help="open target and capture a live auth session to a file",
    )
    p_harvest.add_argument(
        "url", nargs="?", default=None, help="page URL to open in the browser"
    )
    p_harvest.add_argument(
        "--target",
        default=None,
        help="URL/host substring identifying the API request to capture "
        "(default: derived from URL)",
    )
    p_harvest.add_argument(
        "--cookie",
        default=None,
        help=f"auth cookie name that must accompany the token "
        f"(known: {', '.join(_KNOWN_AUTH_COOKIES)})",
    )
    p_harvest.add_argument(
        "--header",
        default="Authorization",
        help="auth header to capture (default: Authorization)",
    )
    p_harvest.add_argument(
        "-o",
        "--out",
        default="session.json",
        help="output session file (default: session.json)",
    )
    p_harvest.add_argument(
        "--borrow",
        default=None,
        help="seed cookies from this real profile before navigating",
    )
    p_harvest.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="seconds to wait for the authenticated request (default: 120)",
    )
    _add_engine_flags(p_harvest)
    p_harvest.set_defaults(func=cmd_harvest)

    # score
    p_score = sub.add_parser(
        "score",
        help="run the reCAPTCHA-v3 reputation score check",
        description="Measure this identity's reCAPTCHA-v3 reputation score "
        "(0.0=bot .. 1.0=warmed) via cleantalk's tester. v3 has no solver — a "
        "low score means you should fall back to identity borrowing.",
    )
    p_score.add_argument(
        "--url",
        default=_DEFAULT_SCORE_URL,
        help="score-test page recorded in JSON output (the detector uses "
        f"cleantalk's tester regardless). default: {_DEFAULT_SCORE_URL}",
    )
    p_score.add_argument(
        "--json", action="store_true", help="emit raw JSON result"
    )
    _add_engine_flags(p_score)
    p_score.set_defaults(func=cmd_score)

    # detect
    p_detect = sub.add_parser(
        "detect", help="identify the WAAP / anti-bot stack on a URL"
    )
    p_detect.add_argument("url", help="URL to probe")
    p_detect.add_argument(
        "--json", action="store_true", help="emit raw JSON result"
    )
    p_detect.set_defaults(func=cmd_detect)

    # agent
    p_agent = sub.add_parser(
        "agent",
        help="open an agent browser to a URL and print the indexed snapshot",
        description="Drive the agent perception layer: navigate through any "
        "WAAP, auto-dismiss consent banners, and print a browser-use-style "
        "indexed snapshot of the page's interactive elements.",
    )
    p_agent.add_argument("url", help="URL to open")
    p_agent.add_argument(
        "--json", action="store_true", help="emit the snapshot as JSON"
    )
    p_agent.add_argument(
        "--no-wait",
        action="store_true",
        help="do not hold the browser open waiting for Enter (headed only)",
    )
    _add_engine_flags(p_agent)
    p_agent.set_defaults(func=cmd_agent)

    # mcp
    p_mcp = sub.add_parser(
        "mcp",
        help="run the MCP server (stdio) exposing the agent over tools",
        description="Start the Wraith MCP server over stdio. Wire it into an "
        "MCP client, e.g. `claude mcp add wraith -- uv run --directory "
        "/path/to/wraith wraith mcp`.",
    )
    p_mcp.set_defaults(func=cmd_mcp)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nwraith: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
