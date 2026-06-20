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

__all__ = [
    "Engine",
    "Session",
    "WraithEngineError",
    "EngineUnavailableError",
    "PlaywrightVersionError",
    "launch",
    "browser",
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
