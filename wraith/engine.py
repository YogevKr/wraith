"""wraith.engine — stealth browser launcher and engine selection.

This module is the front door to Wraith's stealth automation. It launches the
best available anti-detection browser and hands back a ready-to-drive
Playwright (sync) ``BrowserContext`` + ``Page``.

Two engines are supported, in priority order:

1. **Camoufox** (PRIMARY) — a hardened Firefox build driven through
   ``camoufox.sync_api``. Firefox is the *right* engine for reputation- and
   fingerprint-based defenses (reCAPTCHA-v3, Reblaze/Link11 ``ac_v2``, ...):
   the challenge's ``isChrome()`` branch is ``false`` under Firefox, so the
   entire Chrome-specific detection cluster (``window.chrome === undefined``
   while the UA claims Chrome, ``HeadlessChrome`` UA, ``$cdc_``/``__*driver_*``
   leaks) is simply skipped. Empirically Camoufox **headless** scores *higher*
   than headed on general bot benchmarks (~90% on techinz/browsers-benchmark).

2. **patchright-Chromium** (FALLBACK) — patched Playwright Chromium via
   ``patchright.sync_api``. The patchright backend suppresses the
   ``Runtime.enable`` CDP leak. We additionally force ``no_viewport=True`` (the
   Playwright default ``1280x720`` viewport is a red flag on
   rebrowser-bot-detector) and strip the ``--enable-automation`` and
   ``--enable-unsafe-swiftshader`` default args.

Critical compatibility note
---------------------------
Camoufox 0.4.x **crashed** when paired with ``playwright >= 1.60`` because of a
Firefox ``pageError`` serialization bug (``coreBundle.js`` reading
``pageError.location.url``). Camoufox 0.5.x fixed it: from 0.5.6 on, the package
ships a browser/playwright floor table and only needs the browser build that
matches the resolved Playwright. Wraith pins ``playwright == 1.62.x`` and the
Camoufox browser build its package pairs with, and still refuses a Playwright
above the package's tested range rather than let it explode mid-run. See
:class:`PlaywrightVersionError`.

Usage
-----
Plain (manual lifetime — you must call ``.close()`` on the returned session)::

    from wraith.engine import launch

    session = launch(engine="auto", headless=True, geoip=True)
    session.page.goto("https://example.com")
    ...
    session.close()

Context manager (auto-closes everything)::

    from wraith.engine import browser

    with browser(engine="camoufox", humanize=True) as session:
        session.page.goto("https://example.com")
        print(session.page.title())
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .proxy import to_playwright_proxy

# Public engine identifiers.
Engine = Literal["auto", "camoufox", "chromium"]

# The last playwright minor that Camoufox 0.5.x can safely drive. Camoufox
# 0.5.9b5 declares ``playwright < 1.63``; 0.5.6 fixed the 0.4.x pageError crash
# for 1.60+, so the old < 1.60 ceiling no longer applies. We require < 1.63 for
# the camoufox engine and let the package's browser/playwright floor table pick
# the matching browser build.
_CAMOUFOX_MAX_PLAYWRIGHT = (1, 63)

# Camoufox 0.4.x and 0.5.x generate navigator.appCodeName. Camoufox 156 removed
# that property from its browser schema, so an unpinned fetch fails during config
# validation before the browser can start. Keep the package and browser pair on
# the last compatible schema until the launcher mapping moves.
# 0.5.9b5 pairs with browser 158.0-beta.40 (see its browser-pin.json).
_CAMOUFOX_BROWSER_VERSION = "158.0-beta.40"
_CAMOUFOX_BROWSER_FETCH = f"official/{_CAMOUFOX_BROWSER_VERSION}"

# Default args we strip from the Chromium fallback. --enable-automation paints a
# giant "I am a bot" sign; --enable-unsafe-swiftshader forces a software GL
# renderer that is a softer-but-real server-side tell.
_CHROMIUM_STRIP_ARGS = ["--enable-automation", "--enable-unsafe-swiftshader"]

# Chromium identity. Playwright's bundled build is Chrome for Testing, whose
# Client Hints brands lack "Google Chrome" (a bot-detector red flag), so an
# installed Google Chrome is preferred. Without one, headless runs the
# "chromium" channel: full Chromium in the new headless mode, not the headless
# shell (no WebGL, no window.chrome, HeadlessChrome in the brands). Both still
# say HeadlessChrome in the User-Agent; the launcher replaces that token.
_CHROMIUM_HEADLESS_CHANNEL = "chromium"
_HEADLESS_UA_TOKEN = "HeadlessChrome/"
_HEADLESS_UA_CACHE: dict[tuple[Any, Any], Optional[str]] = {}

# Headless windows default to 1280x720, Playwright's well-known viewport, with
# a screen no larger than the page. Shape them like a maximized desktop
# window instead: a 1920x1080 screen and the page below 87px of browser UI.
_HEADLESS_SCREEN = (1920, 1080)
_HEADLESS_BROWSER_UI_HEIGHT = 87

# Chromium options that belong to launch(), not to the browser context.
_CHROMIUM_LAUNCH_ONLY = frozenset(
    {
        "channel",
        "args",
        "proxy",
        "env",
        "executable_path",
        "slow_mo",
        "timeout",
        "chromium_sandbox",
    }
)

# Clearance-cookie names by WAAP vendor. The presence of any one of these in
# the context's cookie jar means the defense handed out a pass and the page is
# (or is about to be) cleared:
#   * Reblaze/Link11 : waap_id, rbzid
#   * Akamai         : _abck, bm_sz
#   * DataDome       : datadome
#   * Incapsula/Imperva : visid_incap, reese84
_DEFAULT_CLEARANCE_COOKIES = (
    "waap_id",
    "rbzid",
    "_abck",
    "bm_sz",
    "datadome",
    "visid_incap",
    "reese84",
)

# Reblaze status tiers that are NOT solvable challenges.
_WAAP_RATE_LIMIT_STATUSES = frozenset({474, 481})
_WAAP_HARD_BLOCK_STATUS = 492
# After a press-and-hold challenge was on screen, its "access denied" text is
# not a hard block for this long: a hold that worked redirects the page, and
# the new page can commit before Wraith sees the redirect's response.
_HOLD_SETTLE_S = 5.0

# The Camoufox launch kwarg through which Firefox ``about:config`` prefs are set.
# A caller may pass their own ``firefox_user_prefs`` (e.g. the un-partition prefs
# that let .google.com reputation cookies reach the 3rd-party reCAPTCHA iframe —
# see :data:`wraith.recaptcha_v3.UNPARTITION_PREFS`). The engine must *merge*
# such a dict into whatever prefs the camoufox-routing code itself sets, never
# silently drop it and never let it destroy Camoufox's own essential prefs.
_FIREFOX_PREFS_KEY = "firefox_user_prefs"

__all__ = [
    "Engine",
    "Session",
    "WraithEngineError",
    "EngineUnavailableError",
    "PlaywrightVersionError",
    "WaapRateLimitedError",
    "WaapHardBlockError",
    "WaapChallengeTimeout",
    "NavigationFailedError",
    "launch",
    "browser",
    "clear_challenge",
    "playwright_version",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class WraithEngineError(RuntimeError):
    """Base class for engine launch failures."""


class EngineUnavailableError(WraithEngineError):
    """A requested engine (or its dependency) could not be imported/launched."""


class PlaywrightVersionError(WraithEngineError):
    """Installed Playwright is incompatible with the selected engine.

    Raised when the Camoufox engine is requested while ``playwright >= 1.60``
    is installed, which triggers the Firefox ``pageError`` serialization crash.
    """


class WaapRateLimitedError(WraithEngineError):
    """The WAAP served an IP rate-limit tier instead of a solvable challenge.

    Maps to Reblaze's HTTP **474 / 481** responses, which are returned *instead
    of* the ``ac_v2`` 247 JS challenge once an exit IP has been hammered. The
    challenge is never even served, so there is nothing the engine can solve and
    no clearance cookie will ever appear.

    This is a *reputation-of-IP* problem, independent of engine choice or
    cookies. The only mitigation is a rotating **residential** proxy plus
    backoff — switching engines or clearing cookies will not help.
    """


class WaapHardBlockError(WraithEngineError):
    """The WAAP hard-blocked the request as obviously non-browser.

    Maps to Reblaze's HTTP **492** response, served when the request looks like
    a bot at the network layer — most commonly a ``HeadlessChrome`` token in the
    User-Agent or another headless/automation leak. No challenge is served, so
    there is nothing to solve.

    Fix the identity leak (use Camoufox/Firefox, drop ``HeadlessChrome`` from
    the UA, avoid automation tells) rather than retrying.
    """


class NavigationFailedError(WraithEngineError):
    """The browser could not load the URL at all.

    ``goto()`` failed and no main-frame response arrived, for example a DNS,
    TLS, or connection error. The page still shows the previous document, so
    Wraith reports the failure instead of treating that document as cleared.
    """


class WaapChallengeTimeout(WraithEngineError):
    """A WAAP clearance cookie never appeared within the allotted time.

    The navigation neither settled on a clean 200 nor produced any known
    clearance cookie before the timeout elapsed. Common causes:

    * wrong engine for the defense (a Chrome engine facing an ``ac_v2`` 247
      challenge that fingerprints ``isChrome()`` — use Camoufox/Firefox);
    * the challenge simply needs longer (raise ``timeout``);
    * you actually saw a 247 and the solver is too slow this run;
    * you actually saw a 474/481 and are silently rate-limited (rotate a
      residential proxy).
    """


# --------------------------------------------------------------------------- #
# Session handle
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    """A live stealth browser session.

    Attributes:
        page: The primary :class:`~playwright.sync_api.Page` to drive.
        context: The owning :class:`~playwright.sync_api.BrowserContext`.
            Use this for ``add_cookies`` (identity borrowing) and
            ``context.on("request", ...)`` (token harvesting).
        browser: The :class:`~playwright.sync_api.Browser`, or ``None`` when a
            persistent context was launched (persistent contexts have no
            separate Browser object).
        engine: Which engine actually launched ("camoufox" or "chromium").
        headless: Whether the session is running headless.

    The session owns its resources. Call :meth:`close` (or use the
    :func:`browser` context manager) to tear everything down cleanly.
    """

    page: Any
    context: Any
    browser: Optional[Any]
    engine: str
    headless: bool
    #: True when the engine humanizes its own pointer motion (Camoufox
    #: ``humanize``). Wraith then skips its extra ``human_move`` before a click.
    humanize: bool = False
    # Internal teardown hooks (innermost first), e.g. stopping the Playwright
    # context manager that Camoufox owns.
    _closers: list = field(default_factory=list, repr=False)
    _closed: bool = field(default=False, repr=False)

    def close(self) -> None:
        """Close the page, context, browser, and any backing resources.

        Idempotent and best-effort: a failure closing one layer does not stop
        the others from being torn down.
        """
        if self._closed:
            return
        self._closed = True

        # Close context (and browser, if any) first, then run engine closers.
        for target in (self.context, self.browser):
            if target is None:
                continue
            with contextlib.suppress(Exception):
                target.close()

        for closer in reversed(self._closers):
            with contextlib.suppress(Exception):
                closer()

    # Allow `with launch(...) as session:` too, not just the `browser()` CM.
    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Version helpers
# --------------------------------------------------------------------------- #
def playwright_version() -> tuple[int, ...]:
    """Return the installed Playwright version as a tuple, e.g. ``(1, 55, 0)``.

    Falls back to ``(0, 0, 0)`` if the version cannot be determined.
    """
    try:
        from importlib.metadata import version

        raw = version("playwright")
    except Exception:
        return (0, 0, 0)

    parts: list[int] = []
    for chunk in raw.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if not num:
            break
        parts.append(int(num))
    return tuple(parts) if parts else (0, 0, 0)


def _assert_camoufox_playwright_ok() -> None:
    """Raise :class:`PlaywrightVersionError` if Playwright is too new for Camoufox."""
    ver = playwright_version()
    if ver >= _CAMOUFOX_MAX_PLAYWRIGHT:
        pretty = ".".join(map(str, ver)) or "unknown"
        raise PlaywrightVersionError(
            f"Camoufox 0.5.x is incompatible with playwright {pretty}.\n"
            f"Camoufox 0.5.9b5 declares playwright < "
            f"{'.'.join(map(str, _CAMOUFOX_MAX_PLAYWRIGHT))}. A newer Playwright can "
            "change the Juggler protocol the browser speaks without a matching "
            "browser build.\n"
            "Fix one of:\n"
            "  - pin playwright < "
            f"{'.'.join(map(str, _CAMOUFOX_MAX_PLAYWRIGHT))}"
            ":  pip install 'playwright==1.62.0'\n"
            "  - upgrade camoufox, which may raise the ceiling\n"
            "  - use engine='chromium' (patchright) instead, which is unaffected."
        )


def _camoufox_browser_options() -> dict[str, Any]:
    """Return the repository-pinned Camoufox browser selection."""
    return {
        "browser": _CAMOUFOX_BROWSER_VERSION,
        # Keep the generated Firefox identity aligned with the selected build.
        "ff_version": 158,
    }


def _camoufox_pinned_executable() -> str:
    """Return the executable for the pinned browser without auto-fetching."""
    try:
        from camoufox.multiversion import find_installed_version
        from camoufox.pkgman import launch_path
    except ImportError as exc:  # pragma: no cover - package guard
        raise EngineUnavailableError(
            "Wraith requires Camoufox 0.5.6 for pinned browser installs."
        ) from exc

    browser_path = find_installed_version(_CAMOUFOX_BROWSER_VERSION)
    if browser_path is None:
        raise _camoufox_missing_error()
    try:
        return launch_path(browser_path)
    except Exception as exc:
        raise _camoufox_missing_error() from exc


def _ensure_camoufox_bundle_properties() -> None:
    """Make a macOS app bundle's ``properties.json`` findable either place.

    Camoufox 0.5.9b5 reads ``properties.json`` from ``Contents/Resources`` for a
    ``Contents/MacOS`` executable. A bundle that carries the file beside the
    executable is read from there as well, so an older layout keeps working.
    """
    try:
        from camoufox import utils as camoufox_utils
    except ImportError:  # pragma: no cover - package guard
        return

    original = camoufox_utils._load_properties
    if getattr(original, "_wraith_bundle_fallback", False):
        return

    def _load_properties(path: Any = None) -> Any:
        if path:
            from pathlib import Path

            path = Path(path)
            beside = path.parent / "properties.json"
            resources = path.parent.parent / "Resources"
            if beside.exists():
                # 0.5.9b5 prefers Resources for a MacOS/named executable.
                # Parse the beside file directly so that layout still wins.
                with open(beside, "rb") as fh:
                    return {p["property"]: p for p in json.load(fh)}
            if (resources / "properties.json").exists():
                path = resources / path.name
        return original(path=path)

    _load_properties._wraith_bundle_fallback = True  # type: ignore[attr-defined]
    camoufox_utils._load_properties = _load_properties


def _camoufox_missing_error() -> EngineUnavailableError:
    return EngineUnavailableError(
        f"Camoufox browser {_CAMOUFOX_BROWSER_VERSION} is not installed. "
        f"Run `camoufox fetch {_CAMOUFOX_BROWSER_FETCH}` first."
    )


def _prepare_camoufox_options(opts: dict[str, Any], extra: dict[str, Any]) -> None:
    """Add caller options and the repository's browser selection."""
    opts.update(extra)
    if "executable_path" not in opts:
        opts["executable_path"] = _camoufox_pinned_executable()
    opts.update(_camoufox_browser_options())
    if "executable_path" in opts and "browser" in opts:
        # Camoufox 0.5.9b5 rejects both. The executable is already the exact
        # pinned build, so the selector is redundant; drop it.
        opts.pop("browser")


def _enter_camoufox(cm: Any) -> Any:
    """Enter Camoufox and turn missing pinned builds into a setup error."""
    try:
        with warnings.catch_warnings():
            # ff_version matches the pinned build (see _camoufox_browser_options);
            # Camoufox warns about any ff_version, so this one is a false alarm.
            warnings.filterwarnings("ignore", message="Spoofing the Firefox version")
            return cm.__enter__()
    except Exception as exc:
        message = str(exc)
        if "not installed" in message or (
            "Browser version" in message and "not found" in message
        ):
            raise _camoufox_missing_error() from exc
        raise


# --------------------------------------------------------------------------- #
# Camoufox (PRIMARY)
# --------------------------------------------------------------------------- #
_PROFILE_FINGERPRINT_FILE = ".wraith_fingerprint.json"


def _profile_fingerprint_preset(profile_dir: str) -> Optional[dict[str, Any]]:
    """Load or create one stable Camoufox fingerprint preset for a profile dir.

    Camoufox draws a fresh device fingerprint on every launch. With a
    persistent profile the cookies survive but the device changes, so a site
    can correlate two visits to the same login as two devices. Store one
    bundled device preset beside the profile and reuse it. Returns ``None``
    (fall back to Camoufox's random draw) when generation is unavailable.
    """
    from pathlib import Path

    path = Path(profile_dir) / _PROFILE_FINGERPRINT_FILE
    with contextlib.suppress(Exception):
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict) and data:
                return data
    try:
        from camoufox.fingerprints import get_random_preset

        version = int(_CAMOUFOX_BROWSER_VERSION.split(".", 1)[0])
        preset = get_random_preset(os=_host_os_family(), ff_version=version)
    except Exception:
        return None
    if not isinstance(preset, dict) or not preset:
        return None
    with contextlib.suppress(Exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(preset))
    return preset


def _is_socks_proxy(proxy: Any) -> bool:
    """Whether a Playwright proxy dict points at a SOCKS server."""
    if not proxy:
        return False
    server = ""
    with contextlib.suppress(Exception):
        server = str(dict(proxy).get("server") or "").lower()
    return server.startswith(("socks4", "socks5"))


def _launch_camoufox(
    *,
    headless: bool,
    geoip: bool,
    locale: Optional[str],
    timezone: Optional[str],
    humanize: bool | float,
    profile_dir: Optional[str],
    extra: dict[str, Any],
) -> Session:
    """Launch Camoufox (Firefox stealth) and return a :class:`Session`."""
    _assert_camoufox_playwright_ok()

    try:
        from camoufox.sync_api import Camoufox
    except ImportError as exc:  # pragma: no cover - import-guard
        raise EngineUnavailableError(
            "Camoufox is not installed. Install it with: pip install 'camoufox[geoip]'"
        ) from exc
    _ensure_camoufox_bundle_properties()

    opts: dict[str, Any] = {
        # os="auto" is not a literal Camoufox value; passing os=None makes
        # Camoufox randomly pick among windows/macos/linux. Pin to the *host*
        # family for identity consistency (a macOS box claiming Windows is a
        # subtle but real tell on some checks).
        "os": _host_os_family(),
        "geoip": geoip,
        "headless": headless,
    }

    # humanize: True enables human-like cursor movement; a float caps the max
    # cursor travel time in seconds. Either is accepted by Camoufox.
    if humanize:
        opts["humanize"] = humanize

    if locale:
        opts["locale"] = locale
    elif geoip and not extra.get("proxy"):
        # geoip draws a random language for the IP's country on every launch
        # (Israel: Hebrew, English, Arabic, Russian, ...), so pages switch
        # language between runs. Without a proxy the IP is this computer's,
        # and its own locale is stable and what its real browsers show.
        own = _system_locale()
        if own:
            opts["locale"] = own

    # Camoufox takes timezone via its fingerprint `config` dict, not a top-level
    # kwarg. geoip already derives a coherent timezone; only override when the
    # caller explicitly asks for one.
    if timezone:
        config = dict(extra.pop("config", {}) or {})
        config["timezone"] = timezone
        opts["config"] = config

    # Persistent profile -> persistent context. Camoufox forwards unknown
    # kwargs (user_data_dir) straight to firefox.launch_persistent_context.
    if profile_dir:
        opts["persistent_context"] = True
        opts["user_data_dir"] = profile_dir
        # One device per profile: keep the fingerprint stable across runs.
        # A caller's own fingerprint or preset wins.
        if "fingerprint" not in extra and "fingerprint_preset" not in extra:
            preset = _profile_fingerprint_preset(profile_dir)
            if preset:
                opts["fingerprint_preset"] = preset

    # firefox_user_prefs needs a dict-MERGE, not the override that a plain
    # opts.update(extra) would do: a caller may pass un-partition prefs (so
    # .google.com reputation cookies reach the 3rd-party reCAPTCHA iframe — see
    # wraith.recaptcha_v3.UNPARTITION_PREFS) and we must add those to, not
    # replace, any prefs the routing code above already set. Pull both sides out
    # first so the bulk update below can't clobber the merged result.
    caller_prefs = extra.pop(_FIREFOX_PREFS_KEY, None)
    base_prefs = opts.pop(_FIREFOX_PREFS_KEY, None)
    merged_prefs = _merge_firefox_prefs(base_prefs, caller_prefs)

    # A SOCKS proxy must resolve DNS remotely, or Firefox resolves the name
    # locally and the IP of the exit and the resolver can contradict.
    if _is_socks_proxy(extra.get("proxy")):
        merged_prefs.setdefault("network.proxy.socks_remote_dns", True)

    # Caller options and the pinned browser must reach validation together.
    _prepare_camoufox_options(opts, extra)

    # Only set firefox_user_prefs when there is actually something to set, so we
    # don't hand Camoufox an empty dict when no prefs were requested either side.
    if merged_prefs:
        opts[_FIREFOX_PREFS_KEY] = merged_prefs

    cm = Camoufox(**opts)
    # Camoufox is itself a context manager that owns the Playwright lifetime.
    # Drive it manually so the Session can own teardown.
    browser_or_ctx = _enter_camoufox(cm)

    if profile_dir:
        # Persistent context: the returned object IS the BrowserContext.
        context = browser_or_ctx
        browser_obj = None
        page = context.pages[0] if context.pages else context.new_page()
    else:
        browser_obj = browser_or_ctx
        context = browser_obj.new_context()
        page = context.new_page()

    return Session(
        page=page,
        context=context,
        browser=browser_obj,
        engine="camoufox",
        headless=headless,
        humanize=bool(humanize),
        _closers=[lambda: cm.__exit__(None, None, None)],
    )


# --------------------------------------------------------------------------- #
# patchright Chromium (FALLBACK)
# --------------------------------------------------------------------------- #
def _chromium_geoip_identity(
    proxy: Any,
) -> tuple[Optional[str], Optional[str]]:
    """Locale and timezone for the exit IP, for the Chromium fallback.

    Camoufox derives these from the IP itself. Chromium does not, so a proxied
    Chromium run otherwise advertises the *host's* locale and timezone under a
    foreign exit IP -- a hard fingerprint contradiction that score-based
    defenses read as masking. Best-effort: any failure (no geoip extra, no
    network, unknown IP) returns ``(None, None)`` and the caller keeps its
    defaults.
    """
    try:
        from camoufox.geolocation import get_geolocation  # type: ignore
        from camoufox.ip import Proxy, public_ip  # type: ignore
    except Exception:  # pragma: no cover - guarded in _launch_chromium earlier
        return None, None
    try:
        proxy_url = None
        if proxy:
            fields = {
                k: v
                for k, v in dict(proxy).items()
                if k in ("server", "username", "password", "bypass")
            }
            proxy_url = Proxy(**fields).as_string()
        geo = get_geolocation(public_ip(proxy_url))
    except Exception:
        return None, None
    language = getattr(geo.locale, "language", None)
    region = getattr(geo.locale, "region", None)
    tag = "-".join(part for part in (language, region) if part) or None
    return tag, (geo.timezone or None)


def _host_has_gpu() -> bool:
    """Whether the host can give Chromium a hardware GL renderer.

    macOS and Windows always can. A Linux host is screened by its DRM render
    node. When unsure, assume a GPU (keep the native renderer).
    """
    if sys.platform.startswith(("darwin", "win")):
        return True
    from pathlib import Path

    with contextlib.suppress(Exception):
        return any(Path("/dev/dri").glob("renderD*"))
    return True


def _chromium_gpu_args(launch_kw: dict[str, Any], headless: bool) -> dict[str, Any]:
    """Keep WebGL available on a headless host with no GPU.

    Wraith strips ``--enable-unsafe-swiftshader`` to avoid a software renderer
    on a real GPU. On a GPU-less host that leaves WebGL null, which is itself a
    headless tell. Force ANGLE/SwiftShader there. A caller GL choice wins.
    """
    if not headless or _host_has_gpu():
        return {}
    args = [str(arg) for arg in launch_kw.get("args") or []]
    if any(arg.startswith(("--use-gl", "--use-angle")) for arg in args):
        return {}
    return {
        "args": [
            *args,
            "--use-gl=angle",
            "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader",
        ]
    }


def _chromium_webrtc_proxy_args(launch_kw: dict[str, Any]) -> dict[str, Any]:
    """Force WebRTC through the proxy so the real IP cannot leak.

    Without this, Chromium's WebRTC stack still gathers host/STUN candidates
    from the host network, so a proxy-backed session can present a public IP
    that contradicts its HTTP exit IP. ``disable_non_proxied_udp`` routes all
    WebRTC traffic through the proxy. A caller-supplied policy wins.
    """
    if not launch_kw.get("proxy"):
        return {}
    args = [str(arg) for arg in launch_kw.get("args") or []]
    if any(arg.startswith("--webrtc-ip-handling-policy") for arg in args):
        return {}
    return {
        "args": [
            *(launch_kw.get("args") or []),
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        ]
    }


def _launch_chromium(
    *,
    headless: bool,
    geoip: bool = False,
    locale: Optional[str],
    timezone: Optional[str],
    profile_dir: Optional[str],
    extra: dict[str, Any],
) -> Session:
    """Launch patched Chromium (patchright) and return a :class:`Session`.

    Stealth hardening applied:
      * patchright backend (suppresses the ``Runtime.enable`` CDP leak),
      * ``no_viewport=True`` (avoids the 1280x720 tell; in Playwright for
        Python ``viewport=None`` means "use the default" and does not),
      * ``ignore_default_args`` strips ``--enable-automation`` and
        ``--enable-unsafe-swiftshader``,
      * the renderer sandbox stays on where Chrome supports it,
      * an installed Google Chrome is used when present (``channel="chrome"``);
        without one, headless runs full Chromium (``channel="chromium"``)
        instead of the headless shell,
      * headless removes ``HeadlessChrome`` from the User-Agent and sizes the
        window like a maximized 1920x1080 desktop window.

    Caller ``channel``, ``executable_path``, ``chromium_sandbox``,
    ``user_agent``, ``viewport``, ``screen`` and ``--window-size`` options
    win over these defaults.
    """
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - import-guard
        raise EngineUnavailableError(
            "patchright is not installed. Install it with: pip install patchright"
        ) from exc

    pw = sync_playwright().start()

    # firefox_user_prefs is a Camoufox/Firefox-only knob; it is meaningless to
    # Chromium and would explode if forwarded to launch()/new_context(). Drop it
    # silently so a caller can pass the same kwargs to either engine. (This is
    # the "ignored for the chromium engine" half of the merge contract.)
    extra.pop(_FIREFOX_PREFS_KEY, None)

    # Split caller kwargs into launch-level vs context-level so we can place
    # them correctly for both persistent and non-persistent launches.
    launch_kw = {k: v for k, v in extra.items() if k in _CHROMIUM_LAUNCH_ONLY}
    context_kw = {k: v for k, v in extra.items() if k not in _CHROMIUM_LAUNCH_ONLY}

    # Chromium has no geoip of its own; derive the exit IP's locale/timezone so a
    # proxied run does not advertise the host's. An explicit caller value wins.
    if geoip and (locale is None or timezone is None):
        geo_locale, geo_timezone = _chromium_geoip_identity(launch_kw.get("proxy"))
        locale = locale or geo_locale
        timezone = timezone or geo_timezone

    # Context-level identity options.
    if locale:
        context_kw.setdefault("locale", locale)
    if timezone:
        context_kw.setdefault("timezone_id", timezone)

    # Keep WebRTC off the host network when a proxy is in use.
    launch_kw.update(_chromium_webrtc_proxy_args(launch_kw))
    # Keep WebGL present on a GPU-less headless host.
    launch_kw.update(_chromium_gpu_args(launch_kw, headless))

    try:
        context, browser_obj, page = _open_chromium(
            pw, headless, profile_dir, launch_kw, context_kw
        )
    except Exception:
        with contextlib.suppress(Exception):
            pw.stop()
        raise

    return Session(
        page=page,
        context=context,
        browser=browser_obj,
        engine="chromium",
        headless=headless,
        _closers=[pw.stop],
    )


def _chromium_sandbox_default() -> bool:
    """Whether Chrome's renderer sandbox works on this host.

    Playwright turns the sandbox off by default (``--no-sandbox``), which
    leaves untrusted pages one renderer bug away from the host. Chrome refuses
    to sandbox as root on Linux; a failed start falls back to no sandbox.
    """
    if sys.platform.startswith(("darwin", "win")):
        return True
    geteuid = getattr(os, "geteuid", None)
    return geteuid is None or geteuid() != 0


def _installed_chrome() -> Optional[str]:
    """Path of an installed Google Chrome (stable) in its standard place."""
    if sys.platform.startswith("darwin"):
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    elif sys.platform.startswith("win"):
        roots = ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
        candidates = [
            os.path.join(
                os.environ[root], "Google", "Chrome", "Application", "chrome.exe"
            )
            for root in roots
            if os.environ.get(root)
        ]
    else:
        candidates = ["/opt/google/chrome/chrome"]
    return next((path for path in candidates if os.path.exists(path)), None)


def _chromium_launch_defaults(
    headless: bool, launch_kw: dict[str, Any]
) -> dict[str, Any]:
    """Launch options Wraith adds unless the caller chose its own."""
    defaults: dict[str, Any] = {}
    if "chromium_sandbox" not in launch_kw:
        defaults["chromium_sandbox"] = _chromium_sandbox_default()
    if "channel" not in launch_kw and "executable_path" not in launch_kw:
        if _installed_chrome():
            defaults["channel"] = "chrome"
        elif headless:
            defaults["channel"] = _CHROMIUM_HEADLESS_CHANNEL
    return defaults


def _shapes_headless_window(
    headless: bool, launch_kw: dict[str, Any], context_kw: dict[str, Any]
) -> bool:
    """Whether to give a headless window a desktop size (caller sizes win)."""
    if not headless or {"viewport", "screen", "no_viewport"} & context_kw.keys():
        return False
    return not any(
        str(arg).startswith("--window-size") for arg in launch_kw.get("args") or []
    )


def _clean_headless_user_agent(
    pw: Any, launch_kw: dict[str, Any], browser_obj: Any = None
) -> Optional[str]:
    """Return the browser's own User-Agent with ``HeadlessChrome`` replaced.

    The value comes from a blank page, so it keeps the real platform and
    version. Setting it as the context ``user_agent`` covers pages, workers
    and request headers, and keeps the Client Hints version list intact (a
    ``--user-agent`` flag empties it). ``None`` when there is nothing to fix.
    """
    key = (launch_kw.get("channel"), launch_kw.get("executable_path"))
    if key not in _HEADLESS_UA_CACHE:
        probe = browser_obj
        if probe is None:
            probe_kw = {k: v for k, v in launch_kw.items() if k != "proxy"}
            probe = pw.chromium.launch(
                headless=True, ignore_default_args=_CHROMIUM_STRIP_ARGS, **probe_kw
            )
        try:
            blank = probe.new_context()
            try:
                user_agent = blank.new_page().evaluate("() => navigator.userAgent")
            finally:
                blank.close()
        finally:
            if browser_obj is None:
                probe.close()
        cleaned = None
        if _HEADLESS_UA_TOKEN in user_agent:
            cleaned = user_agent.replace(_HEADLESS_UA_TOKEN, "Chrome/")
        _HEADLESS_UA_CACHE[key] = cleaned
    return _HEADLESS_UA_CACHE[key]


def _open_chromium(
    pw: Any,
    headless: bool,
    profile_dir: Optional[str],
    launch_kw: dict[str, Any],
    context_kw: dict[str, Any],
) -> tuple[Any, Any, Any]:
    """Start Chromium with Wraith's defaults and return ``(context, browser, page)``.

    If the defaults stop the browser from starting (no full Chromium install,
    no sandbox support), start it with the caller's options only.
    """
    defaults = _chromium_launch_defaults(headless, launch_kw)
    try:
        return _open_chromium_once(
            pw, headless, profile_dir, {**launch_kw, **defaults}, dict(context_kw)
        )
    except Exception as exc:
        if not defaults:
            raise
        warnings.warn(
            f"Chromium did not start with {defaults} ({exc}); retrying without them",
            RuntimeWarning,
            stacklevel=3,
        )
        return _open_chromium_once(
            pw, headless, profile_dir, launch_kw, dict(context_kw)
        )


def _open_chromium_once(
    pw: Any,
    headless: bool,
    profile_dir: Optional[str],
    launch_kw: dict[str, Any],
    context_kw: dict[str, Any],
) -> tuple[Any, Any, Any]:
    if profile_dir:
        context = _open_persistent_chromium(
            pw, headless, profile_dir, launch_kw, context_kw
        )
        page = context.pages[0] if context.pages else context.new_page()
        return context, None, page
    browser_obj = pw.chromium.launch(
        headless=headless,
        ignore_default_args=_CHROMIUM_STRIP_ARGS,
        **launch_kw,
    )
    try:
        context = _new_chromium_context(
            pw, browser_obj, headless, launch_kw, context_kw
        )
        return context, browser_obj, context.new_page()
    except Exception:
        with contextlib.suppress(Exception):
            browser_obj.close()
        raise


def _open_persistent_chromium(
    pw: Any,
    headless: bool,
    profile_dir: str,
    launch_kw: dict[str, Any],
    context_kw: dict[str, Any],
) -> Any:
    """Launch a persistent-profile context (launch and context in one call)."""
    if _shapes_headless_window(headless, launch_kw, context_kw):
        # The browser window itself gets the size, so outer and inner sizes
        # differ by the browser UI, as on a real desktop.
        width, height = _HEADLESS_SCREEN
        args = [*(launch_kw.get("args") or [])]
        args += [
            f"--window-size={width},{height}",
            f"--screen-info={{0,0 {width}x{height}}}",
        ]
        launch_kw = {**launch_kw, "args": args}
    if headless and "user_agent" not in context_kw:
        # The context takes its User-Agent at launch, so read it from a short
        # probe browser first (cached per binary).
        user_agent = _clean_headless_user_agent(pw, launch_kw)
        if user_agent:
            context_kw["user_agent"] = user_agent
    # no_viewport=True shows the real window size, unless the caller forced
    # a viewport.
    if "viewport" not in context_kw:
        context_kw.setdefault("no_viewport", True)
    return pw.chromium.launch_persistent_context(
        profile_dir,
        headless=headless,
        ignore_default_args=_CHROMIUM_STRIP_ARGS,
        **launch_kw,
        **context_kw,
    )


def _new_chromium_context(
    pw: Any,
    browser_obj: Any,
    headless: bool,
    launch_kw: dict[str, Any],
    context_kw: dict[str, Any],
) -> Any:
    """Open the session context in a launched browser."""
    if _shapes_headless_window(headless, launch_kw, context_kw):
        # New contexts open 1280x720 windows whatever the launch flags say,
        # so emulate the desktop screen and page size instead.
        width, height = _HEADLESS_SCREEN
        inner = height - _HEADLESS_BROWSER_UI_HEIGHT
        context_kw["viewport"] = {"width": width, "height": inner}
        context_kw["screen"] = {"width": width, "height": height}
    if headless and "user_agent" not in context_kw:
        user_agent = _clean_headless_user_agent(pw, launch_kw, browser_obj)
        if user_agent:
            context_kw["user_agent"] = user_agent
    # Playwright emulates a 1280x720 viewport and screen by default, a known
    # rebrowser-bot-detector red flag. In Python, viewport=None is the same as
    # leaving it out; no_viewport=True turns the emulation off, so the page
    # sees the real window and screen.
    if "viewport" not in context_kw:
        context_kw.setdefault("no_viewport", True)
    return browser_obj.new_context(**context_kw)


# --------------------------------------------------------------------------- #
# Misc helpers
# --------------------------------------------------------------------------- #
def _merge_firefox_prefs(
    base: Optional[Mapping[str, Any]],
    incoming: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Shallow-merge two ``firefox_user_prefs`` dicts, ``incoming`` winning ties.

    Used so a caller-supplied ``firefox_user_prefs`` (e.g.
    :data:`wraith.recaptcha_v3.UNPARTITION_PREFS`) is *added to* — not substituted
    for — any prefs the camoufox-routing code itself sets, rather than one dict
    clobbering the other. Either side may be ``None``/empty. Never raises: a
    non-mapping argument is treated as empty.

    A flat (shallow) merge is correct here because ``firefox_user_prefs`` is a
    flat ``{pref_name: value}`` map — each key is an independent ``about:config``
    entry, so last-writer-wins per key is the desired semantics. ``incoming``
    (the caller's prefs) wins on key collisions so callers can intentionally
    override an engine default.
    """
    merged: dict[str, Any] = {}
    if isinstance(base, Mapping):
        merged.update(base)
    if isinstance(incoming, Mapping):
        merged.update(incoming)
    return merged


def _system_locale() -> Optional[str]:
    """This computer's language tag (``en-US``), or ``None`` when unknown."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        tag = os.environ.get(name, "").split(".")[0].split("@")[0]
        if tag and tag not in ("C", "POSIX"):
            return tag.replace("_", "-")
    if sys.platform.startswith("darwin"):
        import subprocess

        with contextlib.suppress(Exception):
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleLocale"],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            tag = out.split("@")[0]
            if tag:
                return tag.replace("_", "-")
    return None


def _host_os_family() -> str:
    """Map the host platform to a Camoufox ``os`` value for identity consistency."""
    if sys.platform.startswith("darwin"):
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def launch(
    engine: Engine = "auto",
    *,
    headless: bool = True,
    geoip: bool = True,
    locale: Optional[str] = None,
    timezone: Optional[str] = None,
    humanize: bool | float = False,
    profile_dir: Optional[str] = None,
    **kw: Any,
) -> Session:
    """Launch the best available stealth browser and return a live :class:`Session`.

    The returned session owns its resources; call :meth:`Session.close` when
    done (or prefer the :func:`browser` context manager, which closes for you).

    Args:
        engine: Which engine to use.
            * ``"auto"`` (default): try Camoufox first, fall back to patchright
              Chromium if Camoufox is unavailable. Note: a Playwright version
              mismatch is treated as fatal for the *explicit* camoufox engine,
              but in ``"auto"`` mode it triggers the fallback (with a warning).
            * ``"camoufox"``: force Camoufox; raises on any problem.
            * ``"chromium"``: force patchright Chromium.
        headless: Run without a visible window. Default ``True`` (Camoufox
            headless actually benchmarks *better* than headed).
        geoip: Derive a coherent timezone/locale/geolocation from the exit IP.
            Strongly recommended for identity consistency. Works for both
            engines: Camoufox derives a full fingerprint identity, and the
            Chromium fallback derives locale + timezone and keeps WebRTC off
            the host network when a proxy is set. An explicit ``locale`` /
            ``timezone`` still wins.
        locale: Override the locale (e.g. ``"he-IL"``). Should match the exit
            region. For Chromium, sets the context ``locale``.
        timezone: Override the IANA timezone (e.g. ``"Asia/Jerusalem"``). For
            Camoufox this is injected into the fingerprint ``config``; for
            Chromium it sets ``timezone_id``.
        humanize: Camoufox-only. ``True`` enables human-like cursor movement; a
            float caps max cursor travel time (seconds). Ignored for Chromium.
        profile_dir: Path to a persistent user-data dir. When given, a
            *persistent context* is launched (cookies/storage survive runs),
            and the returned ``Session.browser`` is ``None``.
        **kw: Extra options forwarded to the underlying engine (e.g. ``proxy``,
            ``block_images``, ``window``/``viewport``, ``fingerprint``,
            ``user_agent``, ``channel``). Engine-appropriate keys are routed
            automatically. ``proxy`` may be a URL string
            (``"http://user:pass@host:port"`` — what :class:`wraith.proxy.ProxyPool`
            and the ``wraith.providers`` classes emit) or an already
            Playwright-shaped ``{"server", "username", "password"}`` dict; a
            string is converted via :func:`wraith.proxy.to_playwright_proxy`
            because Camoufox/Playwright only accept the dict form.

    Returns:
        A :class:`Session` with ``.page`` / ``.context`` ready to drive.

    Raises:
        PlaywrightVersionError: ``engine="camoufox"`` with a playwright at or
            above the Camoufox-tested ceiling.
        EngineUnavailableError: the requested engine (or dependency) is missing,
            or ``"auto"`` exhausted all engines.
        WraithEngineError: other launch failures.
        ValueError: unknown ``engine`` value.
    """
    # Bridge the string proxy currency (providers / ProxyPool / clear_challenge
    # rotation) to the ProxySettings dict both engines require. Done once here
    # so every engine path below sees the same shape.
    if "proxy" in kw:
        kw["proxy"] = to_playwright_proxy(kw["proxy"])

    if engine == "camoufox":
        return _launch_camoufox(
            headless=headless,
            geoip=geoip,
            locale=locale,
            timezone=timezone,
            humanize=humanize,
            profile_dir=profile_dir,
            extra=dict(kw),
        )

    if engine == "chromium":
        return _launch_chromium(
            headless=headless,
            geoip=geoip,
            locale=locale,
            timezone=timezone,
            profile_dir=profile_dir,
            extra=dict(kw),
        )

    if engine == "auto":
        try:
            return _launch_camoufox(
                headless=headless,
                geoip=geoip,
                locale=locale,
                timezone=timezone,
                humanize=humanize,
                profile_dir=profile_dir,
                extra=dict(kw),
            )
        except (EngineUnavailableError, PlaywrightVersionError) as exc:
            warnings.warn(
                f"Camoufox engine unavailable ({exc.__class__.__name__}: {exc}); "
                "falling back to patchright Chromium. Chromium is weaker against "
                "reCAPTCHA-v3 / Reblaze — use a playwright the Camoufox "
                "package supports to use Camoufox.",
                RuntimeWarning,
                stacklevel=2,
            )
            try:
                return _launch_chromium(
                    headless=headless,
                    geoip=geoip,
                    locale=locale,
                    timezone=timezone,
                    profile_dir=profile_dir,
                    extra=dict(kw),
                )
            except EngineUnavailableError as exc2:
                raise EngineUnavailableError(
                    "Both engines failed: Camoufox "
                    f"({exc.__class__.__name__}: {exc}); "
                    f"patchright Chromium ({exc2})."
                ) from exc2

    raise ValueError(
        f"unknown engine {engine!r}; expected 'auto', 'camoufox', or 'chromium'"
    )


@contextlib.contextmanager
def browser(
    engine: Engine = "auto",
    *,
    headless: bool = True,
    geoip: bool = True,
    locale: Optional[str] = None,
    timezone: Optional[str] = None,
    humanize: bool | float = False,
    profile_dir: Optional[str] = None,
    **kw: Any,
):
    """Context-manager wrapper around :func:`launch` that auto-closes the session.

    Yields a :class:`Session`. All arguments are identical to :func:`launch`.

    Example::

        with browser(engine="camoufox", geoip=True) as s:
            s.page.goto("https://example.com")
            print(s.page.title())
    """
    session = launch(
        engine,
        headless=headless,
        geoip=geoip,
        locale=locale,
        timezone=timezone,
        humanize=humanize,
        profile_dir=profile_dir,
        **kw,
    )
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# WAAP challenge clearing
# --------------------------------------------------------------------------- #
def _cookie_pairs(cookies: Any) -> list[tuple[str, str]]:
    """Best-effort extract ``(name, value)`` pairs from a ``cookies()`` list.

    Duck-typed: accepts a list of dicts (the Playwright shape) or anything that
    yields mappings/objects with ``name``/``value`` fields. Never raises. The
    value defaults to ``""`` when absent.
    """
    pairs: list[tuple[str, str]] = []
    try:
        for ck in cookies or []:
            if isinstance(ck, dict):
                name = ck.get("name")
                value = ck.get("value", "")
            else:
                name = getattr(ck, "name", None)
                value = getattr(ck, "value", "")
            if name:
                pairs.append((str(name), "" if value is None else str(value)))
    except Exception:
        pass
    return pairs


def _cookie_names(cookies: Any) -> set[str]:
    """Best-effort extract cookie *names* from a Playwright ``cookies()`` list.

    Duck-typed: accepts a list of dicts (the Playwright shape) or anything that
    yields mappings/objects with a ``name`` field. Never raises.
    """
    return {name for name, _ in _cookie_pairs(cookies)}


def _load_detect() -> Any:
    """Import :mod:`wraith.detect` lazily, returning ``None`` on failure.

    Imported lazily (inside callers) to avoid a module-level import cycle:
    ``detect`` may itself reach for engine helpers. A failed/partial import is
    non-fatal — callers fall back to the engine's own built-in defaults.
    """
    try:
        from wraith import detect as _detect  # local import: break import cycle
        return _detect
    except Exception:  # pragma: no cover - defensive (detect should import)
        return None


def _default_clearance_cookies(detect: Any) -> set[str]:
    """The default set of clearance-cookie names.

    Prefers :data:`detect.CLEARANCE_COOKIES` (the single source of truth — a
    ``dict[vendor -> list[name]]`` whose union is the usable default) when the
    detect module exposes it; otherwise falls back to this module's own
    :data:`_DEFAULT_CLEARANCE_COOKIES` tuple so the engine stays self-contained
    if ``detect`` is older / partially built.
    """
    table = getattr(detect, "CLEARANCE_COOKIES", None) if detect is not None else None
    if isinstance(table, Mapping):
        union: set[str] = set()
        for names in table.values():
            try:
                union.update(str(n) for n in (names or []))
            except TypeError:
                continue
        if union:
            return union
    return set(_DEFAULT_CLEARANCE_COOKIES)


def _cookie_is_valid(detect: Any, name: str, value: str) -> bool:
    """Is this clearance cookie actually in a *solved* (pass-granting) state?

    Delegates to :func:`detect.cookie_is_valid` when available (the single
    source of truth). Otherwise applies the engine's built-in rule, whose only
    non-trivial case is **Akamai ``_abck``**:

    For most vendors, *presence* of the cookie is sufficient. But Akamai's
    ``_abck`` is set on the very first response in an *unsolved* state whose
    value contains the sentinel ``~-1~``; it only flips to a cleared/solved
    state once the sensor POST is accepted, at which point the value contains
    ``~0~`` (and no longer ``~-1~``). So a fresh ``_abck`` must NOT be treated
    as cleared — we require ``~0~`` present and ``~-1~`` absent.
    """
    if detect is not None:
        fn = getattr(detect, "cookie_is_valid", None)
        if callable(fn):
            try:
                return bool(fn(name, value))
            except Exception:
                pass
    # Built-in fallback rule.
    if name == "_abck":
        return ("~0~" in value) and ("~-1~" not in value)
    return True


def _pool_next(proxy_pool: Any) -> Optional[str]:
    """Advance the pool and return the next proxy URL, or ``None``. Never raises.

    Duck-typed against :class:`wraith.proxy.ProxyPool.next`.
    """
    if proxy_pool is None:
        return None
    nxt = getattr(proxy_pool, "next", None)
    if not callable(nxt):
        return None
    try:
        result = nxt()
    except Exception:
        return None
    return str(result) if result else None


def _pool_mark_bad(proxy_pool: Any, proxy: str) -> None:
    """Mark a proxy as bad on the pool so it is skipped. Never raises.

    Duck-typed against :class:`wraith.proxy.ProxyPool.mark_bad`.
    """
    if proxy_pool is None:
        return
    mark = getattr(proxy_pool, "mark_bad", None)
    if callable(mark):
        with contextlib.suppress(Exception):
            mark(proxy)


def _behavioral_nudge(page: Any) -> None:
    """Best-effort human-like activity to lift behavioural scores. Never raises.

    A short cursor move + dwell nudges score-based defenses (Akamai Bot
    Manager, DataDome, PerimeterX/HUMAN) toward "human" while the challenge is
    being evaluated. Wrapped so a missing ``behavior`` module, a headless quirk,
    or an engine without a real mouse is non-fatal.
    """
    try:
        from wraith import behavior as _behavior  # local: keep import light
    except Exception:
        return
    with contextlib.suppress(Exception):
        _behavior.human_move(page)
    with contextlib.suppress(Exception):
        # A wheel notch adds the input channel a hand always produces on a
        # challenge page; sensors read the wheel stream, not just the cursor.
        # Scroll back so the page view is unchanged for perception.
        page.mouse.wheel(0, 120)
        page.wait_for_timeout(60)
        page.mouse.wheel(0, -120)
    with contextlib.suppress(Exception):
        # A *short* dwell — deliberately briefer than behavior.dwell()'s default
        # 0.4-1.8s so the nudge never dominates a small `timeout` budget.
        _behavior.dwell(0.1, 0.3)


def _raise_if_not_loaded(url: str, error: Optional[Exception], status: Any) -> None:
    """Raise when ``goto()`` failed and no main-frame response arrived.

    The page then still shows the previous document, which must not pass as a
    cleared target.
    """
    if error is None or status is not None:
        return
    reason = (str(error).strip().splitlines() or [type(error).__name__])[0]
    raise NavigationFailedError(f"Navigation to {url} failed: {reason}")


def _looks_like_challenge_page(content: str, title: str = "") -> bool:
    """Whether the rendered document still looks like a JS challenge wall.

    A challenge served with HTTP 200 and a large body otherwise passes as
    "real content", so :func:`clear_challenge` can return before the challenge
    JS has run. Match only high-precision Cloudflare markers; the widget may
    stay on the page after it clears, so this alone is not proof, but with no
    clearance cookie it is a reason to keep waiting. Never raises.
    """
    hay = (str(title or "") + "  " + str(content or "")[:8000]).lower()
    return any(marker in hay for marker in _CHALLENGE_PAGE_MARKERS)


def _hold_settling(holding: bool, seen_at: Optional[float]) -> bool:
    """True while a press-and-hold challenge is on screen, or was at
    ``seen_at`` and may still be redirecting (see ``_HOLD_SETTLE_S``)."""
    import time

    if holding:
        return True
    return seen_at is not None and time.monotonic() - seen_at < _HOLD_SETTLE_S


# A Cloudflare Turnstile checkbox sits at this fraction of the widget frame.
# The frame is cross-origin, so a coordinate click is the only way in. Measured
# on the widget geometry; it holds for the compact and normal layouts.
_TURNSTILE_COORD = (0.11, 0.45)
_TURNSTILE_ATTEMPTS = 2

# High-precision markers of a Cloudflare managed-challenge document (not a
# standalone Turnstile widget, which does not carry them).
_CHALLENGE_PAGE_MARKERS = (
    "challenge-platform",
    "cf-chl-",
    "cf-challenge",
    "cf-please-wait",
    "just a moment",
    "attention required! | cloudflare",
)


def _click_visible_turnstile(page: Any) -> bool:
    """Click the checkbox of a visible Cloudflare Turnstile widget once.

    Finds a live ``challenges.cloudflare.com`` turnstile frame, aims at the
    checkbox fraction of its box with a human path, and clicks. Returns
    ``True`` when a click was sent. Never raises.
    """
    try:
        frames = list(getattr(page, "frames", []) or [])
    except Exception:
        return False
    for frame in frames:
        url = getattr(frame, "url", "") or ""
        if "challenges.cloudflare.com" not in url or "turnstile" not in url.lower():
            continue
        try:
            box = frame.frame_element().bounding_box()
        except Exception:
            continue
        if not box or box.get("width", 0) < 60 or box.get("height", 0) < 20:
            continue
        # The hidden duplicate sits fully above the viewport; skip it.
        if box.get("y", 0) < 0 or box.get("y", 0) > 100000:
            continue
        x = box["x"] + _TURNSTILE_COORD[0] * box["width"]
        y = box["y"] + _TURNSTILE_COORD[1] * box["height"]
        try:
            from wraith import behavior as _behavior

            with contextlib.suppress(Exception):
                _behavior.human_move(page, target=(x, y))
            page.mouse.click(x, y, delay=70)
            return True
        except Exception:
            continue
    return False


def clear_challenge(
    url: str,
    *,
    session: Optional[Session] = None,
    engine: Engine = "auto",
    timeout: float = 30.0,
    clearance_cookies: Optional["list[str] | tuple[str, ...]"] = None,
    settle: float = 1.0,
    proxy_pool: Optional["Any"] = None,
    press_hold: bool = True,
    navigate: bool = True,
    require_clearance: bool = False,
    **launch_kw: Any,
) -> Session:
    """Navigate to ``url`` and return a Session once the WAAP challenge clears.

    This is the general-purpose, *cookie-free* WAAP front door. It handles the
    family of JS interstitial challenges that a real browser engine can solve on
    its own — most notably **Reblaze/Link11 ``ac_v2``** — and returns a
    ready-to-drive :class:`Session` once a clearance cookie has been issued (or,
    for an ordinary site with no WAAP at all, once the page has simply loaded).

    How ``ac_v2`` clears (no cookies required)
    ------------------------------------------
    Reblaze's handshake is::

        GET url            -> HTTP 247  (JS challenge: window.rbzns{seed,
                                          bereshit:'1'} + winsocks())
        <browser solves it>             (fingerprint + seed-keyed SHA-1 hashcash)
        GET token          -> HTTP 248  + Set-Cookie waap_id
        <retry>            -> HTTP 200  (cleared)

    A fresh **Camoufox/Firefox** context solves this natively because Firefox
    skips the Chrome-specific ``isChrome()`` detection cluster — so it needs no
    warmed identity and no borrowed cookies. We just navigate, then poll the
    cookie jar until a clearance cookie (``waap_id``/``rbzid``/...) appears.

    Non-solvable tiers
    ------------------
    Two responses are *not* challenges and cannot be cleared by waiting:

    * **HTTP 474 / 481** — Reblaze IP rate-limit, served *instead of* the 247
      challenge after an exit IP is hammered. Raises
      :class:`WaapRateLimitedError`. The only fix is a rotating **residential**
      proxy (pass ``proxy="http://user:pass@host:port"``) plus backoff —
      engine choice and cookies are irrelevant here.
    * **HTTP 492** — hard block (non-browser / ``HeadlessChrome`` UA). Raises
      :class:`WaapHardBlockError`. Fix the UA/automation leak.

    Non-WAAP sites
    --------------
    If ``url`` has no anti-bot layer, no clearance cookie will ever appear — that
    is success, not failure. Once the navigation settles on a 200 with real
    content, the (unmodified) Session is returned immediately. There is nothing
    to clear. A 200 page that still carries a Cloudflare challenge marker
    (``challenge-platform``, "Just a moment", ...) with no clearance cookie is
    the one exception: it keeps polling until the challenge JS finishes.

    Args:
        url: The URL to navigate to and clear.
        session: An existing :class:`Session` to drive. When ``None`` (default)
            a new one is launched via :func:`launch` and is **owned** by this
            call — it is closed automatically if an error is raised. A
            caller-supplied session is **never** closed here (you own it).
        engine: Engine to use when self-launching (ignored if ``session`` is
            given). For ``ac_v2`` prefer the default ``"auto"`` (Camoufox first).
        timeout: Seconds to wait for a clearance cookie / clean settle before
            raising :class:`WaapChallengeTimeout`.
        clearance_cookies: Override the set of cookie names treated as a
            clearance pass. When ``None`` (default) the set is the union of all
            :data:`wraith.detect.CLEARANCE_COOKIES` vendor entries (the single
            source of truth), falling back to this module's built-in
            ``waap_id, rbzid, _abck, bm_sz, datadome, visid_incap, reese84`` if
            ``detect`` is unavailable. A clearance cookie counts as a pass ONLY
            when :func:`wraith.detect.cookie_is_valid` says its *value* is in a
            solved state — most notably a fresh Akamai ``_abck`` containing the
            ``~-1~`` sentinel is **not** cleared (it must reach ``~0~``).
        settle: Seconds of post-load grace before accepting a clean response
            or a clearance cookie (lets a late challenge swap in). After a
            HUMAN challenge, require a new successful main-frame response; opaque
            PX cookie values alone do not prove acceptance.
        proxy_pool: Optional rotating-proxy pool (a duck-typed object exposing
            ``next() -> str | None``, ``mark_bad(str)`` and ``len()`` — e.g.
            :class:`wraith.proxy.ProxyPool`). Only used when this call **owns**
            the session (``session is None``): on a :class:`WaapRateLimitedError`
            (474/481) or :class:`WaapHardBlockError` (492) — both
            reputation-of-IP problems — the owned session is closed, a fresh one
            is launched on ``proxy_pool.next()``, and the navigation is retried,
            bounded by ``len(proxy_pool)`` attempts (the failing proxy is
            ``mark_bad``-ed first). When a session was *passed in* (we don't own
            it) rotation is skipped — a live session's proxy can't be changed —
            and the error is re-raised unchanged.
        press_hold: Attempt visible HUMAN press-and-hold challenges automatically.
            Make at most two eight-second holds within ``timeout``. ``False``
            disables input attempts but still waits for the challenge to clear.
        navigate: When ``True`` (default) call ``page.goto(url)`` first. When
            ``False`` the caller already moved the page (history back/forward);
            clear the challenge on the page in place and ignore ``url``.
        require_clearance: When ``True``, a clean 200 with content does not count
            as success; only a valid *clearance cookie* does. Use it when the
            caller needs the session truly cleared (for example Akamai
            ``_abck``, whose presence is not validity). ``False`` (default)
            keeps the old behavior: a non-WAAP page returns on clean content.
        **launch_kw: Forwarded to :func:`launch` when self-launching (e.g.
            ``proxy``, ``headless``, ``geoip``, ``locale``, ``timezone``).

    Returns:
        The cleared :class:`Session` (the same object passed in, if any).

    Raises:
        WaapRateLimitedError: top-level navigation returned 474/481 and no
            (further) proxy was available to rotate to.
        WaapHardBlockError: top-level navigation returned 492 and no (further)
            proxy was available to rotate to.
        WaapChallengeTimeout: no valid clearance cookie / clean settle within
            ``timeout`` (the message names any detected WAAP vendor as a hint).
    """
    import time as _time  # local: keep module import-light & duck-typed
    from ._press_hold import PressHoldHandler

    # detect is the single source of truth for clearance-cookie names and for
    # whether a cookie *value* is in a solved state. Imported lazily to avoid an
    # import cycle; falls back to this module's built-ins if unavailable.
    detect = _load_detect()
    wanted = (
        set(clearance_cookies)
        if clearance_cookies
        else _default_clearance_cookies(detect)
    )

    owns_session = session is None

    def _navigate_and_poll(active: Session) -> Session:
        """Drive one session: navigate, classify status, then poll to clearance.

        Returns the (same) session on success. Raises
        :class:`WaapRateLimitedError` / :class:`WaapHardBlockError` on the
        non-solvable tiers (so the caller may rotate a proxy), or
        :class:`WaapChallengeTimeout` when no valid clearance appears.
        """
        nonlocal url
        page = active.page
        context = active.context

        # Capture the TOP-LEVEL navigation response status. We attach the
        # listener BEFORE navigating so we never miss the main document
        # response (sub-resource responses are ignored).
        nav_status: dict[str, Any] = {"status": None, "generation": 0}

        def _on_response(response: Any) -> None:
            try:
                # Only the main-frame document response interests us. Match by
                # URL against the request we issued; fall back to first 2xx-ish
                # main response if frame info isn't available.
                req = getattr(response, "request", None)
                is_nav = bool(getattr(req, "is_navigation_request", lambda: False)()) \
                    if req is not None and callable(getattr(req, "is_navigation_request", None)) \
                    else None
                resp_url = getattr(response, "url", None)
                if is_nav and getattr(req, "frame", None) is not getattr(page, "main_frame", None):
                    return
                if is_nav is True or (is_nav is None and resp_url == url):
                    nav_status["status"] = getattr(response, "status", None)
                    nav_status["generation"] += 1
            except Exception:
                pass

        with contextlib.suppress(Exception):
            page.on("response", _on_response)

        # Navigate. Capture the goto() response too — it is the most reliable
        # source of the top-level status across engines. With ``navigate=False``
        # the caller already moved the page (history back/forward); poll it in
        # place.
        goto_status: Any = None
        goto_error: Optional[Exception] = None
        if navigate:
            try:
                resp = page.goto(url)
                if resp is not None:
                    goto_status = getattr(resp, "status", None)
            except Exception as exc:
                # A navigation that throws (e.g. interstitial abort) is not fatal
                # on its own; fall through to status/cookie polling.
                goto_error = exc
                resp = None
        else:
            url = getattr(page, "url", "") or url

        main_status = goto_status if goto_status is not None else nav_status["status"]

        _raise_if_not_loaded(url, goto_error, main_status)

        # Hard, non-solvable tiers — bail immediately with actionable guidance.
        if main_status in _WAAP_RATE_LIMIT_STATUSES:
            raise WaapRateLimitedError(
                f"{url} returned HTTP {main_status} (Reblaze IP rate-limit tier): "
                "the ac_v2 challenge was NOT served because this exit IP is "
                "rate-limited. This is reputation-of-IP, not engine/cookies. "
                "Rotate a residential proxy (proxy='http://user:pass@host:port') "
                "and back off, then retry."
            )
        if main_status == _WAAP_HARD_BLOCK_STATUS:
            raise WaapHardBlockError(
                f"{url} returned HTTP {main_status} (hard block): the request "
                "looked non-browser at the network layer (commonly a "
                "'HeadlessChrome' User-Agent or another headless/automation "
                "leak). No challenge was served. Use Camoufox/Firefox and "
                "remove the UA/automation tell rather than retrying."
            )

        # Best-effort BEHAVIOURAL NUDGE before polling: a short human-like
        # cursor move + dwell helps score-based defenses (Akamai Bot Manager,
        # DataDome, PerimeterX/HUMAN) tip toward "human" while the challenge is
        # evaluated. Entirely non-fatal.
        _behavioral_nudge(page)

        # Poll: success is either (a) a *valid* clearance cookie appearing, or
        # (b) a clean 200 with real content (non-WAAP site / already cleared).
        deadline = _time.monotonic() + float(timeout)
        clean_since: Optional[float] = None
        hold_handler = PressHoldHandler(enabled=press_hold, deadline=deadline)
        challenge_generation: Optional[int] = None
        challenge_seen_at: Optional[float] = None
        turnstile_attempts = 0
        turnstile_next_at = 0.0

        while True:
            # Read the rendered page once per iteration (reused below).
            try:
                content = page.content()
            except Exception:
                content = ""

            generation_before_poll = nav_status["generation"]
            holding = hold_handler.poll(page, content)
            if holding:
                challenge_seen_at = _time.monotonic()
            if holding and challenge_generation is None:
                challenge_generation = generation_before_poll

            # A visible Turnstile widget needs a click and is cross-origin, so
            # aim at its checkbox fraction. Bounded and paced, never fatal.
            poll_now = _time.monotonic()
            if turnstile_attempts < _TURNSTILE_ATTEMPTS and poll_now >= turnstile_next_at:
                turnstile_next_at = poll_now + 1.0
                if _click_visible_turnstile(page):
                    turnstile_attempts += 1
                    if challenge_generation is None:
                        challenge_generation = generation_before_poll

            # A stale trust cookie does not prove that a visible challenge cleared.
            try:
                jar = context.cookies()
            except Exception:
                jar = []
            cookie_cleared = any(
                name in wanted and name not in {"_px", "_px2", "_px3"}
                and _cookie_is_valid(detect, name, value)
                for name, value in _cookie_pairs(jar)
            )

            # (a2) Hard block? Fail fast — don't burn the whole timeout on a
            # page that will never clear (rotate identity + proxy instead).
            # Skip it when a navigation answered after `content` was read (a
            # hold that just cleared): the next poll reads the new page, and
            # stale content must never count as a block.
            fresh = nav_status["generation"] == generation_before_poll
            title = ""
            if content:
                try:
                    title = page.title()
                except Exception:
                    title = ""
            if detect is not None and content and fresh:
                _is_blocked = getattr(detect, "is_blocked", None)
                if callable(_is_blocked):
                    try:
                        block_reason = _is_blocked(content, title)
                    except Exception:
                        block_reason = None
                    settling = _hold_settling(holding, challenge_seen_at)
                    if settling and block_reason == "PerimeterX/HUMAN access denied":
                        block_reason = None
                    if block_reason:
                        raise WaapHardBlockError(
                            f"{url}: hard block detected ({block_reason}). "
                            "Rotate identity + proxy; do not retry this IP."
                        )

            # (b) settled on a clean 200 with real content?
            now = _time.monotonic()
            if nav_status["status"] is not None:
                main_status = nav_status["status"]
            status_ok = (
                not hold_handler.seen if main_status is None
                else 200 <= int(main_status) < 300
            )
            if challenge_generation is not None:
                # An HTTP 200 challenge response is not proof of acceptance.
                status_ok = status_ok and nav_status["generation"] > challenge_generation
            has_content = bool(content) and len(content) > 200
            # A 200 challenge wall is not real content yet; without a clearance
            # cookie, keep waiting for the challenge JS to finish.
            challenge_page = has_content and _looks_like_challenge_page(content, title)
            if require_clearance:
                cleared = cookie_cleared and not hold_handler.seen
            else:
                cleared = (
                    status_ok and has_content and (cookie_cleared or not challenge_page)
                ) or (cookie_cleared and not hold_handler.seen)
            if not holding and cleared:
                if clean_since is None:
                    clean_since = now
                elif now - clean_since >= float(settle):
                    # Give every success path a grace period for late widgets.
                    # Opaque PX cookies cannot prove server acceptance; after
                    # a HUMAN challenge we require a successful main response.
                    return active
            else:
                clean_since = None

            if now >= deadline:
                seen = main_status if main_status is not None else "unknown"
                if hold_handler.seen:
                    raise WaapChallengeTimeout(
                        f"HUMAN press-and-hold challenge did not clear for {url} "
                        f"within {timeout:.0f}s after {hold_handler.attempts} attempt(s). "
                        "Complete verification in a headed browser or borrow "
                        "a verified session, then retry."
                    )
                # Vendor-aware hint: name any WAAP we can fingerprint on the page
                # so the caller knows which defense outlasted the timeout.
                vendor_hint = ""
                if detect is not None:
                    ident = getattr(detect, "identify_waap", None)
                    if callable(ident):
                        try:
                            vendors = ident(page) or []
                        except Exception:
                            vendors = []
                        if vendors:
                            vendor_hint = (
                                f" Detected WAAP vendor(s): {', '.join(vendors)}."
                            )
                raise WaapChallengeTimeout(
                    f"No valid clearance cookie appeared for {url} within "
                    f"{timeout:.0f}s (last top-level status: {seen}).{vendor_hint} "
                    "Possible causes: wrong engine for this defense (a Chrome "
                    "engine vs an ac_v2 247 challenge — use Camoufox/Firefox); "
                    "the challenge needs longer (raise timeout); a fresh Akamai "
                    "_abck stuck at '~-1~' (sensor POST not accepted yet); if you "
                    "saw a 247 the solver is too slow this run; if you saw a "
                    "474/481 you are silently rate-limited (rotate a residential "
                    "proxy)."
                )

            # Light poll cadence; tolerate engines without wait_for_timeout.
            try:
                page.wait_for_timeout(250)
            except Exception:
                _time.sleep(0.25)

    # ----------------------------------------------------------------------- #
    # Caller-supplied session: we do NOT own it. No proxy rotation possible
    # (can't swap a live session's proxy) and we never close it.
    # ----------------------------------------------------------------------- #
    if not owns_session:
        return _navigate_and_poll(session)  # type: ignore[arg-type]

    # ----------------------------------------------------------------------- #
    # We OWN the session. Launch it (optionally on the first pool proxy) and,
    # on a reputation-of-IP failure (474/481/492), rotate to the next proxy and
    # retry — bounded by len(proxy_pool) attempts. The failing session is closed
    # before each relaunch; the failing proxy is marked bad.
    # ----------------------------------------------------------------------- #
    # Total attempts: 1 base attempt, plus one extra per additional pool proxy.
    try:
        pool_size = len(proxy_pool) if proxy_pool is not None else 0
    except Exception:
        pool_size = 0
    # Total navigation attempts are bounded by the number of pool proxies (a
    # rotation can only ever try one IP per proxy). With no pool there is exactly
    # one attempt. The first attempt draws a pool proxy UNLESS the caller pinned
    # an explicit proxy in launch_kw (which we honour first, then rotate).
    max_attempts = max(1, pool_size)
    explicit_proxy = launch_kw.get("proxy") is not None
    draw_on_first = proxy_pool is not None and not explicit_proxy

    current_proxy: Optional[str] = None
    last_rotatable_exc: Optional[WraithEngineError] = None
    active: Optional[Session] = None

    for attempt in range(max_attempts):
        # Pick this attempt's proxy. The first attempt either honours the
        # caller's explicit proxy (current_proxy stays None — not pool-owned) or
        # draws the first pool proxy; later attempts always draw from the pool.
        if attempt > 0 or draw_on_first:
            current_proxy = _pool_next(proxy_pool)
            if current_proxy is None:
                break  # pool exhausted — surface the last rotatable error
            launch_kw = {**launch_kw, "proxy": current_proxy}

        try:
            active = launch(engine=engine, **launch_kw)
        except WraithEngineError:
            # A launch failure on a pool proxy: mark it bad before propagating.
            if current_proxy is not None and proxy_pool is not None:
                _pool_mark_bad(proxy_pool, current_proxy)
            raise

        try:
            return _navigate_and_poll(active)
        except (WaapRateLimitedError, WaapHardBlockError) as exc:
            # Reputation-of-IP failure. Tear down this session; if the pool has
            # another proxy, rotate and retry. Otherwise re-raise.
            last_rotatable_exc = exc
            with contextlib.suppress(Exception):
                active.close()
            active = None
            if current_proxy is not None and proxy_pool is not None:
                _pool_mark_bad(proxy_pool, current_proxy)
            if proxy_pool is None or attempt + 1 >= max_attempts:
                raise
            # else: loop to the next proxy.
        except BaseException:
            # Any other failure (timeout, etc.): close the owned session and
            # propagate — rotation only helps reputation-of-IP tiers.
            with contextlib.suppress(Exception):
                active.close()
            raise

    # Pool exhausted without clearing. Re-raise the last reputation-of-IP error.
    if last_rotatable_exc is not None:
        raise last_rotatable_exc
    # Defensive: should be unreachable (max_attempts >= 1 always runs the body).
    raise WaapChallengeTimeout(  # pragma: no cover
        f"Exhausted all {max_attempts} proxy attempt(s) clearing {url} "
        "without a result."
    )
