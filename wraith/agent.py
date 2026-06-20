"""wraith.agent — a browser-use-style perception/action layer over the stealth engine.

This module turns Wraith's stealth :class:`~wraith.engine.Session` into an
*agent-friendly* surface: an LLM (or any caller) drives the browser by reading a
flat, indexed :class:`~wraith.snapshot.Snapshot` of the interactive elements on
the page and then acting on those elements **by integer index** — ``click(12)``,
``type(7, "hello")`` — instead of hand-crafting CSS/XPath selectors.

The indexing contract is owned by :mod:`wraith.snapshot`:
:func:`~wraith.snapshot.take_snapshot` walks the DOM, finds the interactive
elements, assigns each a sequential integer index, and stamps
``data-wraith-index="<i>"`` onto the live DOM node. This class acts on those
stamped attributes via ``page.locator('[data-wraith-index="<i>"]')`` — so a
snapshot must be reasonably fresh for an index to resolve (every mutating action
re-snapshots so the indices the caller sees are always current).

Why this sits on top of the stealth engine (and not vanilla Playwright)
----------------------------------------------------------------------
:meth:`AgentBrowser.navigate` does not merely ``page.goto`` — it routes the
navigation through :func:`wraith.engine.clear_challenge`, so any WAAP
interstitial (Reblaze/Link11 ``ac_v2``, Akamai, DataDome, ...) is solved by the
real browser engine *before* the agent ever sees the page. It then auto-dismisses
the usual cookie/consent banners (English + Hebrew) so the first snapshot the
agent reads is the actual content, not a consent wall.

Usage
-----
Owning its own session::

    from wraith.agent import agent_browser

    with agent_browser(engine="camoufox", headless=True) as ab:
        snap = ab.navigate("https://example.com")
        print(snap.to_text())
        ab.click(3)
        ab.type(1, "wraith", enter=True)
        print(ab.read())

Reusing an existing stealth session (identity already borrowed, etc.)::

    from wraith import launch
    from wraith.agent import AgentBrowser

    session = launch(engine="camoufox")
    # ... inject_cookies(session.context, ...) to borrow an identity ...
    ab = AgentBrowser(session=session)        # we do NOT own this session
    ab.navigate("https://app.example.com")
    ...
    ab.close()                                # closes the agent, NOT the session
    session.close()
"""

from __future__ import annotations

import contextlib
from typing import Any, Optional

from . import engine as _engine
from .snapshot import Snapshot, take_snapshot

__all__ = ["AgentBrowser", "agent_browser"]


# Buttons we click to dismiss cookie / consent / "I understand" interstitials.
# Matched case-insensitively against the element's accessible text. Hebrew:
# מאשר ("approve"), אישור ("confirmation").
_CONSENT_TEXT_RE = r"(?i)\b(accept|agree|got it|i understand|allow all|ok)\b|מאשר|אישור"

# A short, defensive list of common consent-button selectors used by the major
# CMPs (OneTrust, Cookiebot, Quantcast, ...) as a fallback to the text match.
_CONSENT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "button#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyButtonAccept",
    ".qc-cmp2-summary-buttons button[mode='primary']",
    "button[aria-label*='accept' i]",
    "button[aria-label*='agree' i]",
    "[data-testid='cookie-accept']",
    "[data-cookiebanner='accept_button']",
)


class AgentBrowser:
    """An agent-facing wrapper around a stealth :class:`~wraith.engine.Session`.

    The browser is perceived through indexed :class:`~wraith.snapshot.Snapshot`
    objects and driven by integer index. Most action methods return a *fresh*
    snapshot taken after the action settles, so the caller always works against
    current indices.

    Ownership: if you pass an existing ``session`` we **borrow** it and never
    close it (you own its lifetime). If you let us launch one lazily, we own it
    and :meth:`close` (or the context manager) tears it down.

    Attributes:
        last_snapshot: The most recent :class:`~wraith.snapshot.Snapshot` taken,
            or ``None`` before the first snapshot.
    """

    def __init__(
        self,
        session: Optional[Any] = None,
        *,
        engine: str = "auto",
        **launch_kw: Any,
    ) -> None:
        """Create an agent browser.

        Args:
            session: An existing :class:`~wraith.engine.Session` to drive. When
                given, it is **borrowed** (never closed by this object). When
                ``None``, a session is launched lazily on first use via
                :func:`wraith.engine.launch`.
            engine: Engine to use when self-launching ("auto"/"camoufox"/
                "chromium"). Ignored when ``session`` is supplied.
            **launch_kw: Extra kwargs forwarded to :func:`wraith.engine.launch`
                when self-launching (e.g. ``headless``, ``geoip``, ``locale``,
                ``proxy``, ``profile_dir``).
        """
        self._session: Optional[Any] = session
        self._owns_session: bool = session is None
        self._engine: str = engine
        self._launch_kw: dict[str, Any] = dict(launch_kw)
        self._closed: bool = False
        self.last_snapshot: Optional[Snapshot] = None

    # ------------------------------------------------------------------ #
    # Session / page plumbing
    # ------------------------------------------------------------------ #
    @property
    def session(self) -> Any:
        """The live :class:`~wraith.engine.Session`, launching one if needed.

        Raises:
            RuntimeError: if this :class:`AgentBrowser` has been closed.
        """
        if self._closed:
            raise RuntimeError("AgentBrowser is closed")
        if self._session is None:
            self._session = _engine.launch(engine=self._engine, **self._launch_kw)
            self._owns_session = True
        return self._session

    @property
    def page(self) -> Any:
        """The primary sync Playwright :class:`Page` of the underlying session."""
        return self.session.page

    @property
    def context(self) -> Any:
        """The underlying :class:`BrowserContext`."""
        return self.session.context

    # ------------------------------------------------------------------ #
    # Perception
    # ------------------------------------------------------------------ #
    def snapshot(self, **kw: Any) -> Snapshot:
        """Take a fresh snapshot of the current page and cache it.

        Args:
            **kw: Forwarded to :func:`wraith.snapshot.take_snapshot`
                (``viewport_only``, ``highlight``, ``max_elements``).

        Returns:
            The new :class:`~wraith.snapshot.Snapshot`, also stored on
            :attr:`last_snapshot`.
        """
        snap = take_snapshot(self.page, **kw)
        self.last_snapshot = snap
        return snap

    def navigate(self, url: str) -> Snapshot:
        """Navigate to ``url`` through the WAAP, dismiss consent, and snapshot.

        The navigation is routed through :func:`wraith.engine.clear_challenge`
        using the *existing* session, so any anti-bot interstitial is solved by
        the real engine before perception begins. We then best-effort dismiss
        common cookie/consent banners (English + Hebrew), wait for the page to
        settle, and return a fresh snapshot.

        Args:
            url: The URL to open.

        Returns:
            A :class:`~wraith.snapshot.Snapshot` of the settled page.

        Raises:
            wraith.engine.WaapRateLimitedError: WAAP IP rate-limit tier.
            wraith.engine.WaapHardBlockError: WAAP hard block.
            wraith.engine.WaapChallengeTimeout: challenge never cleared.
        """
        # clear_challenge drives the session we pass and never closes a
        # caller-supplied session — so it is safe to hand it ours regardless of
        # ownership. Returns the same Session object on success.
        _engine.clear_challenge(url, session=self.session)

        self._wait_for_settle()
        self._dismiss_consent()
        self._wait_for_settle()
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Actions (all act by data-wraith-index and re-snapshot)
    # ------------------------------------------------------------------ #
    def click(self, index: int) -> Snapshot:
        """Click the element with the given snapshot ``index``.

        Acts via ``page.locator('[data-wraith-index="<index>"]').click()``,
        which requires the element to still carry the attribute stamped by the
        most recent snapshot.

        Args:
            index: The integer index from the current snapshot.

        Returns:
            A fresh :class:`~wraith.snapshot.Snapshot` taken after the click
            settles.
        """
        self._locator(index).click()
        self._wait_for_settle()
        return self.snapshot()

    def type(
        self,
        index: int,
        text: str,
        *,
        clear: bool = True,
        enter: bool = False,
    ) -> Snapshot:
        """Type ``text`` into the element with the given snapshot ``index``.

        Uses human-like keystroke cadence (:func:`wraith.behavior.human_type`)
        when available, falling back to ``locator.fill``. Optionally clears the
        field first and/or presses Enter afterward.

        Args:
            index: The integer index from the current snapshot.
            text: The text to enter.
            clear: Clear any existing value before typing (default ``True``).
            enter: Press Enter after typing (default ``False``) — useful for
                submitting search boxes.

        Returns:
            A fresh :class:`~wraith.snapshot.Snapshot` taken after the action
            settles.
        """
        locator = self._locator(index)

        if clear:
            with contextlib.suppress(Exception):
                locator.fill("")

        typed = False
        # Prefer human-paced typing for reputation-sensitive fields; degrade to
        # fill() if behavior helpers or per-key typing aren't available.
        try:
            from .behavior import human_type

            human_type(locator, text)
            typed = True
        except Exception:
            typed = False
        if not typed:
            locator.fill(text)

        if enter:
            with contextlib.suppress(Exception):
                locator.press("Enter")

        self._wait_for_settle()
        return self.snapshot()

    def scroll(self, direction: str = "down", amount: int = 700) -> Snapshot:
        """Scroll the page and re-snapshot.

        Args:
            direction: One of ``"down"``, ``"up"``, ``"top"``, ``"bottom"``,
                ``"left"``, ``"right"``. Unknown values scroll down.
            amount: Pixels to scroll for the relative directions
                (down/up/left/right). Ignored for ``top``/``bottom``.

        Returns:
            A fresh :class:`~wraith.snapshot.Snapshot` of the scrolled page.
        """
        amt = int(amount)
        d = (direction or "down").lower()
        if d == "up":
            js = f"window.scrollBy(0, {-amt})"
        elif d == "top":
            js = "window.scrollTo(0, 0)"
        elif d == "bottom":
            js = "window.scrollTo(0, document.body.scrollHeight)"
        elif d == "left":
            js = f"window.scrollBy({-amt}, 0)"
        elif d == "right":
            js = f"window.scrollBy({amt}, 0)"
        else:  # "down" and any unknown value
            js = f"window.scrollBy(0, {amt})"

        with contextlib.suppress(Exception):
            self.page.evaluate(js)
        self._wait_for_settle()
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def read(self) -> str:
        """Return the page's readable content as markdown (or plain text).

        Uses :mod:`markdownify` to convert the rendered HTML to markdown when it
        is importable; otherwise falls back to the page body's visible text.

        Returns:
            A markdown (or plain-text) rendering of the current page.
        """
        try:
            from markdownify import markdownify as _md  # type: ignore

            html = self.page.content()
            return _md(html)
        except Exception:
            # markdownify missing, or content()/conversion failed — fall back to
            # the visible body text, which is always available.
            try:
                return self.page.inner_text("body")
            except Exception:
                return ""

    def get_text(self, index: Optional[int] = None) -> str:
        """Return the text of a single element, or the whole page body.

        Args:
            index: A snapshot index to read the text of. When ``None`` (default),
                returns the visible text of the whole ``<body>``.

        Returns:
            The element's (or body's) visible text. Empty string if it cannot be
            read.
        """
        if index is None:
            try:
                return self.page.inner_text("body")
            except Exception:
                return ""
        try:
            return self._locator(index).inner_text()
        except Exception:
            # Fall back to the cached snapshot's text for this index, if any.
            if self.last_snapshot is not None:
                el = self.last_snapshot.by_index(index)
                if el is not None:
                    return el.text
            return ""

    def screenshot(self, path: Optional[str] = None) -> bytes:
        """Capture a screenshot of the current page.

        Args:
            path: Optional filesystem path to also write the PNG to.

        Returns:
            The PNG image bytes.
        """
        if path is not None:
            return self.page.screenshot(path=path)
        return self.page.screenshot()

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #
    @property
    def current_url(self) -> str:
        """The current page URL (empty string if unavailable)."""
        try:
            return self.page.url
        except Exception:
            return ""

    @property
    def current_title(self) -> str:
        """The current page title (empty string if unavailable)."""
        try:
            return self.page.title()
        except Exception:
            return ""

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """Close the agent. Closes the underlying session only if we own it.

        Idempotent. A borrowed (caller-supplied) session is left untouched.
        """
        if self._closed:
            return
        self._closed = True
        if self._owns_session and self._session is not None:
            with contextlib.suppress(Exception):
                self._session.close()
        # Drop the reference either way; a borrowed session stays alive for its
        # owner.
        self._session = None

    def __enter__(self) -> "AgentBrowser":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _locator(self, index: int) -> Any:
        """Build a Playwright locator for the snapshot ``index``.

        The selector targets the ``data-wraith-index`` attribute stamped onto
        the live DOM by the most recent :func:`wraith.snapshot.take_snapshot`.
        """
        return self.page.locator(f'[data-wraith-index="{int(index)}"]')

    def _wait_for_settle(self) -> None:
        """Best-effort wait for the page to quiesce after a navigation/action.

        Tries a ``networkidle`` wait (bounded), then a DOM-content-loaded wait,
        then a small fixed grace. Never raises — these are convenience waits and
        any failure (timeout, navigation in flight) is non-fatal.
        """
        page = self.page
        for state, timeout in (("domcontentloaded", 5000), ("networkidle", 4000)):
            try:
                page.wait_for_load_state(state, timeout=timeout)
            except Exception:
                # Timed out or no navigation pending; that's fine.
                pass
        with contextlib.suppress(Exception):
            page.wait_for_timeout(150)

    def _dismiss_consent(self) -> None:
        """Best-effort click-through of cookie/consent banners.

        Tries known CMP selectors first, then a text match against buttons /
        ARIA-button roles for accept/agree/"got it"/Hebrew approval words. Only
        clicks *visible* candidates and stops after the first successful click.
        Never raises.
        """
        page = self.page

        # 1) Known CMP selectors (fast, precise).
        for sel in _CONSENT_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=1500)
                    return
            except Exception:
                continue

        # 2) Text/role match against buttons. Playwright's get_by_role with a
        #    regex name matches accessible name case-insensitively.
        import re

        pattern = re.compile(_CONSENT_TEXT_RE)
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=pattern).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=1500)
                    return
            except Exception:
                continue

        # 3) Last resort: any clickable element whose text matches.
        try:
            loc = page.locator("button, [role='button'], a").filter(
                has_text=pattern
            ).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=1500)
        except Exception:
            pass


@contextlib.contextmanager
def agent_browser(
    session: Optional[Any] = None,
    *,
    engine: str = "auto",
    **launch_kw: Any,
):
    """Context-manager factory for an :class:`AgentBrowser`.

    Yields an :class:`AgentBrowser` and closes it on exit (which closes the
    underlying session only if it was self-launched — a passed-in ``session`` is
    left for its owner).

    Args:
        session: An existing :class:`~wraith.engine.Session` to borrow, or
            ``None`` to launch lazily.
        engine: Engine to use when self-launching.
        **launch_kw: Forwarded to :func:`wraith.engine.launch` when
            self-launching.

    Example::

        with agent_browser(engine="camoufox", headless=True) as ab:
            print(ab.navigate("https://example.com").to_text())
    """
    ab = AgentBrowser(session=session, engine=engine, **launch_kw)
    try:
        yield ab
    finally:
        ab.close()
