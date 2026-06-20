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
   ``Runtime.enable`` CDP leak. We additionally force ``viewport=None`` (the
   Playwright default ``1280x720`` viewport is a red flag on
   rebrowser-bot-detector) and strip the ``--enable-automation`` and
   ``--enable-unsafe-swiftshader`` default args.

Critical compatibility note
---------------------------
Camoufox 0.4.x **crashes** when paired with ``playwright >= 1.60`` because of a
Firefox ``pageError`` serialization bug (``coreBundle.js`` reading
``pageError.location.url``). Wraith pins ``playwright == 1.55.x``; this module
detects a mismatch up front and raises a clear, actionable error rather than
letting it explode mid-run. See :class:`PlaywrightVersionError`.

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
import sys
import warnings
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

# Public engine identifiers.
Engine = Literal["auto", "camoufox", "chromium"]

# The last playwright minor that Camoufox 0.4.x can safely drive. From 1.60 on,
# the Firefox pageError serialization path crashes (coreBundle.js reads
# pageError.location.url). We require < 1.60 for the camoufox engine.
_CAMOUFOX_MAX_PLAYWRIGHT = (1, 60)

# Default args we strip from the Chromium fallback. --enable-automation paints a
# giant "I am a bot" sign; --enable-unsafe-swiftshader forces a software GL
# renderer that is a softer-but-real server-side tell.
_CHROMIUM_STRIP_ARGS = ["--enable-automation", "--enable-unsafe-swiftshader"]

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

__all__ = [
    "Engine",
    "Session",
    "WraithEngineError",
    "EngineUnavailableError",
    "PlaywrightVersionError",
    "WaapRateLimitedError",
    "WaapHardBlockError",
    "WaapChallengeTimeout",
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
            f"Camoufox 0.4.x is incompatible with playwright {pretty}.\n"
            f"playwright >= {'.'.join(map(str, _CAMOUFOX_MAX_PLAYWRIGHT))} triggers a "
            "Firefox pageError serialization crash (coreBundle.js reads "
            "pageError.location.url) that hard-crashes the page.\n"
            "Fix one of:\n"
            "  - pin playwright to 1.55.x:  pip install 'playwright==1.55.0'\n"
            "  - run Camoufox in its own virtualenv with playwright 1.55.x\n"
            "  - use engine='chromium' (patchright) instead, which is unaffected."
        )


# --------------------------------------------------------------------------- #
# Camoufox (PRIMARY)
# --------------------------------------------------------------------------- #
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

    # Caller passthrough (proxy, block_images, window, fingerprint, ...).
    opts.update(extra)

    cm = Camoufox(**opts)
    # Camoufox is itself a context manager that owns the Playwright lifetime.
    # Drive it manually so the Session can own teardown.
    browser_or_ctx = cm.__enter__()

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
        _closers=[lambda: cm.__exit__(None, None, None)],
    )


# --------------------------------------------------------------------------- #
# patchright Chromium (FALLBACK)
# --------------------------------------------------------------------------- #
def _launch_chromium(
    *,
    headless: bool,
    locale: Optional[str],
    timezone: Optional[str],
    profile_dir: Optional[str],
    extra: dict[str, Any],
) -> Session:
    """Launch patched Chromium (patchright) and return a :class:`Session`.

    Stealth hardening applied:
      * patchright backend (suppresses the ``Runtime.enable`` CDP leak),
      * ``viewport=None`` / ``no_viewport=True`` (avoids the 1280x720 tell),
      * ``ignore_default_args`` strips ``--enable-automation`` and
        ``--enable-unsafe-swiftshader``.
    """
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - import-guard
        raise EngineUnavailableError(
            "patchright is not installed. Install it with: pip install patchright"
        ) from exc

    pw = sync_playwright().start()

    # Split caller kwargs into launch-level vs context-level so we can place
    # them correctly for both persistent and non-persistent launches.
    launch_only = {"channel", "args", "proxy", "env", "executable_path", "slow_mo", "timeout"}
    launch_kw = {k: v for k, v in extra.items() if k in launch_only}
    context_kw = {k: v for k, v in extra.items() if k not in launch_only}

    # Context-level identity options.
    if locale:
        context_kw.setdefault("locale", locale)
    if timezone:
        context_kw.setdefault("timezone_id", timezone)

    try:
        if profile_dir:
            # Persistent context: launch + context in one call. Use
            # no_viewport=True (the persistent-context equivalent of
            # viewport=None) unless the caller forced a viewport.
            if "viewport" not in context_kw:
                context_kw.setdefault("no_viewport", True)
            context = pw.chromium.launch_persistent_context(
                profile_dir,
                headless=headless,
                ignore_default_args=_CHROMIUM_STRIP_ARGS,
                **launch_kw,
                **context_kw,
            )
            browser_obj = None
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser_obj = pw.chromium.launch(
                headless=headless,
                ignore_default_args=_CHROMIUM_STRIP_ARGS,
                **launch_kw,
            )
            # viewport=None disables Playwright's fixed 1280x720 viewport — a
            # known rebrowser-bot-detector red flag.
            context_kw.setdefault("viewport", None)
            context = browser_obj.new_context(**context_kw)
            page = context.new_page()
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


# --------------------------------------------------------------------------- #
# Misc helpers
# --------------------------------------------------------------------------- #
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
        geoip: For Camoufox, derive a coherent timezone/locale/geolocation from
            the exit IP. Strongly recommended for identity consistency. Ignored
            by the Chromium engine (use ``locale``/``timezone`` explicitly).
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
            automatically.

    Returns:
        A :class:`Session` with ``.page`` / ``.context`` ready to drive.

    Raises:
        PlaywrightVersionError: ``engine="camoufox"`` with ``playwright>=1.60``.
        EngineUnavailableError: the requested engine (or dependency) is missing,
            or ``"auto"`` exhausted all engines.
        WraithEngineError: other launch failures.
        ValueError: unknown ``engine`` value.
    """
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
                "reCAPTCHA-v3 / Reblaze — pin playwright==1.55.x to use Camoufox.",
                RuntimeWarning,
                stacklevel=2,
            )
            try:
                return _launch_chromium(
                    headless=headless,
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
def _cookie_names(cookies: Any) -> set[str]:
    """Best-effort extract cookie *names* from a Playwright ``cookies()`` list.

    Duck-typed: accepts a list of dicts (the Playwright shape) or anything that
    yields mappings/objects with a ``name`` field. Never raises.
    """
    names: set[str] = set()
    try:
        for ck in cookies or []:
            name = None
            if isinstance(ck, dict):
                name = ck.get("name")
            else:
                name = getattr(ck, "name", None)
            if name:
                names.add(str(name))
    except Exception:
        pass
    return names


def clear_challenge(
    url: str,
    *,
    session: Optional[Session] = None,
    engine: Engine = "auto",
    timeout: float = 30.0,
    clearance_cookies: Optional["list[str] | tuple[str, ...]"] = None,
    settle: float = 1.0,
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
    to clear.

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
            clearance pass. Defaults to ``waap_id, rbzid, _abck, bm_sz,
            datadome, visid_incap, reese84``.
        settle: Seconds of post-load grace given to a clean 200 before treating
            it as a cleared / non-WAAP success (lets a late challenge swap in).
        **launch_kw: Forwarded to :func:`launch` when self-launching (e.g.
            ``proxy``, ``headless``, ``geoip``, ``locale``, ``timezone``).

    Returns:
        The cleared :class:`Session` (the same object passed in, if any).

    Raises:
        WaapRateLimitedError: top-level navigation returned 474/481.
        WaapHardBlockError: top-level navigation returned 492.
        WaapChallengeTimeout: no clearance cookie / clean settle within
            ``timeout``.
    """
    wanted = set(clearance_cookies) if clearance_cookies else set(_DEFAULT_CLEARANCE_COOKIES)

    owns_session = session is None
    if owns_session:
        session = launch(engine=engine, **launch_kw)

    # From here on, a self-launched session must be torn down on any error.
    try:
        page = session.page
        context = session.context

        # Capture the TOP-LEVEL navigation response status. We attach the
        # listener BEFORE navigating so we never miss the main document
        # response (sub-resource responses are ignored).
        nav_status: dict[str, Any] = {"status": None}

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
                if is_nav is True or (is_nav is None and resp_url == url):
                    nav_status["status"] = getattr(response, "status", None)
            except Exception:
                pass

        with contextlib.suppress(Exception):
            page.on("response", _on_response)

        # Navigate. Capture the goto() response too — it is the most reliable
        # source of the top-level status across engines.
        goto_status: Any = None
        try:
            resp = page.goto(url)
            if resp is not None:
                goto_status = getattr(resp, "status", None)
        except Exception:
            # A navigation that throws (e.g. interstitial abort) is not fatal on
            # its own; fall through to status/cookie polling.
            resp = None

        main_status = goto_status if goto_status is not None else nav_status["status"]

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

        # Poll: success is either (a) a clearance cookie appearing, or (b) a
        # clean 200 with real content (non-WAAP site / already cleared).
        import time as _time  # local: keep module import-light & duck-typed

        deadline = _time.monotonic() + float(timeout)
        clean_since: Optional[float] = None

        while True:
            # (a) clearance cookie present?
            try:
                jar = context.cookies()
            except Exception:
                jar = []
            if _cookie_names(jar) & wanted:
                return session

            # (b) settled on a clean 200 with real content?
            now = _time.monotonic()
            status_ok = main_status is None or 200 <= int(main_status) < 300
            has_content = False
            if status_ok:
                try:
                    content = page.content()
                    has_content = bool(content) and len(content) > 200
                except Exception:
                    has_content = False
            if status_ok and has_content:
                if clean_since is None:
                    clean_since = now
                elif now - clean_since >= float(settle):
                    # Stable clean page and no clearance cookie => non-WAAP (or
                    # already cleared). Nothing to clear; success.
                    return session
            else:
                clean_since = None

            if now >= deadline:
                seen = main_status if main_status is not None else "unknown"
                raise WaapChallengeTimeout(
                    f"No clearance cookie appeared for {url} within {timeout:.0f}s "
                    f"(last top-level status: {seen}). Possible causes: wrong "
                    "engine for this defense (a Chrome engine vs an ac_v2 247 "
                    "challenge — use Camoufox/Firefox); the challenge needs "
                    "longer (raise timeout); if you saw a 247 the solver is too "
                    "slow this run; if you saw a 474/481 you are silently "
                    "rate-limited (rotate a residential proxy)."
                )

            # Light poll cadence; tolerate engines without wait_for_timeout.
            try:
                page.wait_for_timeout(250)
            except Exception:
                _time.sleep(0.25)
    except BaseException:
        if owns_session and session is not None:
            with contextlib.suppress(Exception):
                session.close()
        raise
