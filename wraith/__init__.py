"""Wraith — the identity-borrowing stealth browser for autonomous agents.

A toolkit that pairs a Firefox-engine stealth browser (Camoufox) with
identity borrowing: rather than trying to beat reputation-based defenses
like reCAPTCHA-v3, it borrows a warmed, already-authenticated identity
from a real browser profile (or harvests live auth tokens) and drives the
target as that user.

Public API (re-exported here for convenience)::

    import wraith

    # launch a stealth browser
    with wraith.browser(engine="camoufox") as s:
        s.page.goto("https://example.com")

    # borrow a warmed identity from a real profile
    profile = wraith.find_zen_profiles()[0]
    cookies = wraith.extract_cookies(profile, domain_filter="example.com")
    wraith.inject_cookies(s.context, cookies)

    # diagnostics
    wraith.identify_waap("https://www.elal.com/")

The symbols below are imported defensively: a missing optional browser
dependency (camoufox / playwright / patchright / httpx) must NOT break a plain
``import wraith``. Anything that fails to import is simply omitted from the
namespace and ``__all__``; ``wraith.missing_imports`` records why.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Names that imported cleanly, assembled into __all__ at the end.
__all__: list[str] = []

# Diagnostic record of any sub-module that failed to import (name -> reason),
# so callers can introspect a partial install without it being fatal.
missing_imports: dict[str, str] = {}


def _reexport(module: str, names: "list[str]") -> None:
    """Import ``wraith.<module>`` and re-export ``names`` into this package.

    Resilient by design: if the sub-module (or one of its optional browser
    deps) cannot be imported, the failure is recorded in ``missing_imports``
    and the names are skipped rather than raising — ``import wraith`` always
    succeeds.
    """
    import importlib

    try:
        mod = importlib.import_module(f".{module}", __name__)
    except Exception as exc:  # ImportError, or a dep error raised at import time
        missing_imports[module] = f"{type(exc).__name__}: {exc}"
        return

    for name in names:
        try:
            globals()[name] = getattr(mod, name)
        except AttributeError as exc:  # pragma: no cover - contract drift guard
            missing_imports[f"{module}.{name}"] = f"AttributeError: {exc}"
            continue
        __all__.append(name)


# Engine: stealth launcher / engine selection (Camoufox primary, patchright
# Chromium fallback).
_reexport(
    "engine",
    [
        "launch",
        "browser",
        "clear_challenge",
        "Session",
        "Engine",
        "WraithEngineError",
        "EngineUnavailableError",
        "PlaywrightVersionError",
        "WaapRateLimitedError",
        "WaapHardBlockError",
        "WaapChallengeTimeout",
        "playwright_version",
    ],
)

# Identity: the signature feature — borrow a warmed identity from a real
# on-disk browser profile instead of beating reputation defenses.
_reexport(
    "identity",
    [
        "Cookie",
        "find_firefox_profiles",
        "find_zen_profiles",
        "find_chrome_profile",
        "extract_cookies",
        "extract_google_reputation",
        "to_playwright_cookies",
        "inject_cookies",
        "to_cookiejar",
        "GOOGLE_REPUTATION_COOKIES",
        "ChromeEncryptionError",
    ],
)

# Harvest: capture live auth tokens (Authorization header + auth cookie) that
# are not recoverable from disk.
_reexport(
    "harvest",
    [
        "SessionHarvester",
        "CapturedSession",
        "harvest_session",
    ],
)

# Detect: self-assessment / target fingerprinting diagnostics.
_reexport(
    "detect",
    [
        "recaptcha_v3_score",
        "recaptcha_params",
        "bot_detector",
        "identify_waap",
        "fingerprint",
        "cookie_is_valid",
        "classify_response",
        "ResponseSignal",
        "CLEARANCE_COOKIES",
        "RECAPTCHA_V3_TEST_URL",
        "BOT_DETECTOR_URL",
    ],
)

# Fast path: no-browser TLS-impersonation replay of a borrowed/harvested session
# (optional — needs curl_cffi; gracefully omitted if absent).
_reexport(
    "fastpath",
    [
        "fetch",
        "replay",
        "from_context",
        "cookies_from_context",
        "classify",
        "ua_to_impersonate",
        "FastPathUnavailableError",
    ],
)

# Proxy: dependency-free proxy pool for clear_challenge rotation.
_reexport(
    "proxy",
    [
        "ProxyPool",
        "normalize_proxy",
    ],
)

# Providers: first-class residential-proxy provider integrations (DataImpulse).
# These build proxy URL strings / ProxyPools that feed engine.launch(proxy=...)
# and clear_challenge(proxy_pool=...).
_reexport(
    "providers",
    [
        "DataImpulse",
        "DataImpulseAuthError",
    ],
)

# Behavior: human-like mouse/keyboard helpers (the supporting act).
_reexport(
    "behavior",
    [
        "human_move",
        "human_type",
        "dwell",
    ],
)

# Snapshot: the agent perception layer — indexed, browser-use-style DOM
# snapshots of the interactive elements on a page.
_reexport(
    "snapshot",
    [
        "take_snapshot",
        "Snapshot",
        "Element",
    ],
)

# Agent: the perceive/act-by-index browser wrapper built on the snapshot layer.
_reexport(
    "agent",
    [
        "AgentBrowser",
        "agent_browser",
    ],
)

# reCAPTCHA: v3 token harvesting from a warmed/borrowed session + solver
# service skeletons (reputation is set at mint; there is no client solver).
_reexport(
    "recaptcha",
    [
        "harvest_token",
        "score",
        "SolverService",
        "CapSolver",
        "TwoCaptcha",
    ],
)

# reCAPTCHA-v3 reputation lifting: the GENERAL "high-score" capability — borrow
# a warmed Google reputation into the 3rd-party reCAPTCHA iframe (the proven
# secure+sameSite=None / un-partitioned-3p-cookie recipe) so the minted v3 score
# clears threshold. ``ensure_high_score`` is the entry point; the reputation
# sources span the risk spectrum (account-free floor -> burner -> primary).
_reexport(
    "recaptcha_v3",
    [
        "ensure_high_score",
        "RecaptchaParams",
        "PlacementSpec",
        "UNPARTITION_PREFS",
        "ReputationSource",
        "BorrowedGoogleCookies",
        "PersistentGrecaptcha",
        "WarmedAccount",
    ],
)

# Always-available metadata.
__all__ += ["__version__", "missing_imports"]
