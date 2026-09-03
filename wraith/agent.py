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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from . import engine as _engine
from .secrets import (
    SecretCapability,
    SecretCapabilityError,
    SecretMaterial,
    SecretPolicyError,
    SecretProvider,
    SecretProviderError,
    SecretRequestContext,
    canonical_origin,
    get_secret_provider,
)
from .snapshot import Snapshot, take_snapshot

__all__ = ["AgentBrowser", "agent_browser", "ClearFailedError"]


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


class ClearFailedError(RuntimeError):
    """:meth:`AgentBrowser.type` could not verify a field was actually cleared.

    Raised instead of silently typing on top of unknown existing content. See
    :meth:`AgentBrowser.type` for the full explanation: some editors (notably
    CodeMirror, whose visible content lives in a JS-owned document model
    behind a hidden ``<textarea>``) let ``locator.fill("")`` complete with no
    exception while leaving the real document untouched, so "no exception" is
    not sufficient signal that a field is actually empty.
    """


@dataclass
class _SecretSessionState:
    """Secret policy state shared by all wrappers for one browser context."""

    uses: dict[str, int] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)
    tainted: bool = False


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
        reputation: Optional[Any] = None,
        secret_providers: Optional[Mapping[str, SecretProvider]] = None,
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
            reputation: An optional
                :class:`wraith.recaptcha_v3.ReputationSource`. When set, the
                self-launched Camoufox engine is started with the
                un-partition firefox prefs
                (:data:`wraith.recaptcha_v3.UNPARTITION_PREFS`) so 3rd-party
                ``google.com`` cookies reach the reCAPTCHA iframe, and every
                :meth:`navigate` runs
                :func:`wraith.recaptcha_v3.ensure_high_score` (after the WAAP is
                cleared and consent dismissed) to inject the reputation cookies
                and lift the reCAPTCHA-v3 score. See that module for the honest
                limits (run-variable score; can't replay tokens). Passing a
                borrowed ``session`` does **not** re-launch it, so for that path
                the caller is responsible for the un-partition prefs — the
                reputation source still primes the existing context on navigate.
            secret_providers: Optional local providers. Local names override
                providers from :func:`wraith.register_secret_provider`.
            **launch_kw: Extra kwargs forwarded to :func:`wraith.engine.launch`
                when self-launching (e.g. ``headless``, ``geoip``, ``locale``,
                ``proxy``, ``profile_dir``).
        """
        self._session: Optional[Any] = session
        self._owns_session: bool = session is None
        self._engine: str = engine
        self._launch_kw: dict[str, Any] = dict(launch_kw)
        self.reputation: Optional[Any] = reputation
        self._secret_providers = dict(secret_providers or {})
        self._closed: bool = False
        self.last_snapshot: Optional[Snapshot] = None

        # When we self-launch with a reputation source, the Camoufox engine must
        # un-partition 3rd-party cookies so the borrowed google.com reputation
        # actually reaches the reCAPTCHA iframe. Merge UNPARTITION_PREFS into any
        # caller-supplied firefox_user_prefs (caller keys win). Done eagerly so
        # the lazy launch in .session picks it up; lazily imported to keep
        # `import wraith.agent` working without the recaptcha_v3 stack.
        if reputation is not None and self._owns_session:
            try:
                from .recaptcha_v3 import UNPARTITION_PREFS

                merged = dict(UNPARTITION_PREFS)
                merged.update(self._launch_kw.get("firefox_user_prefs") or {})
                self._launch_kw["firefox_user_prefs"] = merged
            except Exception:
                # recaptcha_v3 unavailable: fall back gracefully — navigate()
                # will still attempt ensure_high_score, which no-ops on import
                # failure. The score lift may be weaker without the prefs.
                pass

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
        """The *active* sync Playwright :class:`Page`.

        Defaults to the session's primary page; follows :meth:`select_tab` /
        :meth:`new_tab` so perception and actions target the active tab.
        """
        ap = getattr(self, "_active_page", None)
        if ap is not None:
            try:
                if not ap.is_closed():
                    return ap
            except Exception:
                pass
            self._active_page = None
        return self.session.page

    @property
    def context(self) -> Any:
        """The underlying :class:`BrowserContext`."""
        return self.session.context

    # ------------------------------------------------------------------ #
    # Tabs / pages
    # ------------------------------------------------------------------ #
    def tabs(self) -> list[dict]:
        """List open tabs as ``[{index, url, title, active}]``."""
        active = self.page
        out: list[dict] = []
        for i, p in enumerate(self.context.pages):
            try:
                url = p.url
            except Exception:
                url = ""
            try:
                title = p.title()
            except Exception:
                title = ""
            out.append({"index": i, "url": url, "title": title, "active": p is active})
        return out

    def select_tab(self, index: int) -> Snapshot:
        """Make tab ``index`` active and return its snapshot."""
        pages = self.context.pages
        self._active_page = pages[int(index)]
        try:
            self._active_page.bring_to_front()
        except Exception:
            pass
        return self.snapshot()

    def new_tab(self, url: Optional[str] = None) -> Snapshot:
        """Open a new tab (optionally navigating to ``url``), make it active."""
        page = self.context.new_page()
        self._active_page = page
        if url:
            page.goto(url)
            self._wait_for_settle()
        return self.snapshot()

    def close_tab(self, index: int) -> list[dict]:
        """Close tab ``index``; if it was active, fall back to the primary page."""
        pages = self.context.pages
        target = pages[int(index)]
        if getattr(self, "_active_page", None) is target:
            self._active_page = None
        with contextlib.suppress(Exception):
            target.close()
        return self.tabs()

    def save_storage_state(
        self,
        path: str,
        *,
        allow_secret_tainted: bool = False,
    ) -> str:
        """Export the context's cookies + localStorage to a Playwright
        ``storageState`` JSON file — a portable, reusable authenticated session
        (the durable form of an identity-borrowed / challenge-cleared context).
        """
        if self.secret_tainted and not allow_secret_tainted:
            raise SecretPolicyError(
                "Storage export is blocked after a secret fill. "
                "Pass allow_secret_tainted=True to accept the risk."
            )
        self.context.storage_state(path=path)
        return path

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
        allow_secret_tainted = bool(kw.pop("allow_secret_tainted", False))
        if kw.get("highlight") and self.secret_tainted and not allow_secret_tainted:
            raise SecretPolicyError(
                "Highlighted snapshots are blocked after a secret fill. "
                "Pass allow_secret_tainted=True to accept the risk."
            )
        prior = self.last_snapshot
        snap = take_snapshot(self.page, **kw)
        if prior is not None:
            prior_sigs = {e.signature for e in prior.elements}
            for el in snap.elements:
                if el.signature not in prior_sigs:
                    el.is_new = True
        self.last_snapshot = snap
        return snap

    def _page_signature(self) -> Optional[dict]:
        """Cheap pre/post-action page fingerprint for change observation."""
        try:
            return self.page.evaluate(
                """() => {
                    const t = (document.body && document.body.innerText) || '';
                    let h = 0;
                    for (let i = 0; i < t.length; i++) { h = ((h << 5) - h + t.charCodeAt(i)) | 0; }
                    return {
                        url: location.href,
                        n: document.querySelectorAll('a,button,input,select,textarea,[role]').length,
                        h,
                    };
                }"""
            )
        except Exception:
            return None

    def _set_changed(self, pre: Optional[dict], snap: Snapshot) -> None:
        """Annotate ``snap.changed`` with what changed vs the pre-action page."""
        if pre is None:
            return
        post = self._page_signature()
        if post is None:
            return
        parts: list[str] = []
        if pre.get("url") != post.get("url"):
            parts.append(f"url changed -> {post.get('url')}")
        dn = int(post.get("n", 0)) - int(pre.get("n", 0))
        if dn > 0:
            parts.append(f"+{dn} elements")
        elif dn < 0:
            parts.append(f"{dn} elements")
        if not parts and pre.get("h") != post.get("h"):
            parts.append("content changed")
        snap.changed = "; ".join(parts) if parts else "no visible change detected"

    def navigate(self, url: str) -> Snapshot:
        """Navigate to ``url`` through the WAAP, dismiss consent, and snapshot.

        The navigation is routed through :func:`wraith.engine.clear_challenge`
        using the *existing* session, so any anti-bot interstitial is solved by
        the real engine before perception begins. We then best-effort dismiss
        common cookie/consent banners (English + Hebrew), wait for the page to
        settle, and return a fresh snapshot.

        Args:
            url: The URL to open.

        When a ``reputation`` source was supplied at construction, this also
        runs :func:`wraith.recaptcha_v3.ensure_high_score` *after* the WAAP is
        cleared and consent is dismissed — injecting the borrowed reputation
        cookies and (best-effort) confirming a ``/recaptcha/api2/reload``
        request carried them — so a reCAPTCHA-v3 score is minted high before the
        agent acts.

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
        self._ensure_high_score()
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
        pre = self._page_signature()
        self._locator(index).click()
        self._wait_for_settle()
        snap = self.snapshot()
        self._set_changed(pre, snap)
        return snap

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

        Clearing (``clear=True``, the default) is **verified, not assumed**.
        ``locator.fill("")`` is tried first — fast, and correct for ordinary
        ``<input>``/``<textarea>`` fields — then the field's value is read back
        to confirm it actually emptied. This matters because some editors keep
        their real document in a JS-owned model that is decoupled from the
        interacted DOM node's raw value (CodeMirror's classic "hidden textarea"
        input-capture pattern is the concrete case this was found against;
        Monaco and other virtualized editors are architecturally similar):
        ``fill("")`` sets the hidden node's DOM value and fires a generic
        ``input`` event, but the editor doesn't treat that as "the user cleared
        the document", so nothing raises and nothing actually clears. If the
        read-back shows the field is still non-empty, we fall back to a
        keyboard-driven clear — focus, then ``ControlOrMeta+a`` followed by
        ``Backspace``/``Delete`` — whose genuine key events any editor's own
        input handling processes correctly, the way a human clearing the field
        would. We deliberately use ``locator.focus()`` + ``locator.press()``
        here rather than ``locator.click()``: on a CodeMirror-style page the
        rendered overlay can intercept pointer events at the element's
        coordinates and make ``click()`` time out, while ``focus()``/``press()``
        don't require that pointer hit-test. The field is verified empty again
        after the fallback; if it *still* isn't, we raise
        :class:`ClearFailedError` rather than silently typing on top of unknown
        existing content — silently typing into a non-empty field is exactly
        how a CodeMirror JSON editor can end up with new text prepended in
        front of an untouched original document instead of replacing it.

        Args:
            index: The integer index from the current snapshot.
            text: The text to enter.
            clear: Clear any existing value before typing (default ``True``).
                See above for the verified clear/fallback/raise behavior.
            enter: Press Enter after typing (default ``False``) — useful for
                submitting search boxes.

        Returns:
            A fresh :class:`~wraith.snapshot.Snapshot` taken after the action
            settles.

        Raises:
            ClearFailedError: ``clear=True`` and the field could not be
                verified empty after both the ``fill("")`` and keyboard-driven
                clear attempts (or its content could not be read back at all).
        """
        pre = self._page_signature()
        locator = self._locator(index)

        if clear:
            self._clear_verified(locator)

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
        snap = self.snapshot()
        self._set_changed(pre, snap)
        return snap

    def fill_secret(
        self,
        index: int,
        capability: SecretCapability | Mapping[str, Any],
    ) -> Snapshot:
        """Fill one browser field from an opaque provider capability.

        Wraith checks the current origin, field kind, expiry, and use count.
        The provider must authenticate the opaque handle before it returns the
        short-lived value. This method never returns that value.
        """
        if isinstance(capability, Mapping):
            capability = SecretCapability.from_dict(capability)
        if not isinstance(capability, SecretCapability):
            raise SecretCapabilityError("capability has an invalid type")

        origin = canonical_origin(self.current_url)
        if origin not in capability.allowed_origins:
            raise SecretPolicyError("The current origin is not allowed")
        if capability.expires_at is not None:
            now = datetime.now(timezone.utc)
            if now >= capability.expires_at:
                raise SecretCapabilityError("The secret capability has expired")

        usage_key = capability.usage_key
        secret_state = self._secret_state
        prior_limit = secret_state.limits.get(usage_key)
        if prior_limit is not None and capability.max_uses > prior_limit:
            raise SecretCapabilityError("The secret capability use limit increased")
        use_limit = (
            capability.max_uses
            if prior_limit is None
            else min(prior_limit, capability.max_uses)
        )
        secret_state.limits[usage_key] = use_limit
        uses = secret_state.uses.get(usage_key, 0)
        if uses >= use_limit:
            raise SecretCapabilityError("The secret capability is exhausted")

        locator = self._locator(index)
        try:
            element = locator.element_handle()
        except Exception:
            element = None
        if element is None:
            raise SecretPolicyError("The target field is not available")
        metadata = self._secret_field_metadata(element)
        if not self._secret_field_matches(capability.field_kind, metadata):
            raise SecretPolicyError("The target field does not match field_kind")

        context = SecretRequestContext(
            origin=origin,
            frame_origin=origin,
            field_kind=capability.field_kind,
            field_tag=metadata["tag"],
            field_type=metadata["type"],
            autocomplete=metadata["autocomplete"],
            index=int(index),
        )
        provider = self._secret_providers.get(capability.provider)
        if provider is None:
            provider = get_secret_provider(capability.provider)

        provider_failed = False
        material: Any = None
        try:
            material = provider.resolve(capability, context)
        except Exception:
            provider_failed = True
        if provider_failed:
            raise SecretProviderError("The secret provider failed")
        if not isinstance(material, SecretMaterial):
            raise SecretProviderError("The secret provider returned invalid material")

        pre = self._page_signature()
        browser_failed = False
        try:
            if canonical_origin(self.current_url) != origin:
                raise SecretPolicyError("The current origin changed during secret use")
            if capability.expires_at is not None:
                now = datetime.now(timezone.utc)
                if now >= capability.expires_at:
                    raise SecretCapabilityError("The secret capability expired during use")
            current_metadata = self._secret_field_metadata(element)
            if not self._secret_field_matches(capability.field_kind, current_metadata):
                raise SecretPolicyError("The target field changed during secret use")

            # Reserve the use before the browser can receive any secret bytes.
            secret_state.uses[usage_key] = uses + 1
            secret_state.tainted = True
            with contextlib.suppress(Exception):
                element.evaluate(
                    "node => node.setAttribute('data-wraith-secret', 'true')"
                )
            element.fill(material.reveal())
        except (SecretCapabilityError, SecretPolicyError):
            raise
        except Exception:
            browser_failed = True
        finally:
            material.clear()
        if browser_failed:
            raise SecretProviderError("The browser could not fill the secret")

        self._wait_for_settle()
        snap = self.snapshot()
        self._set_changed(pre, snap)
        return snap

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

    def screenshot(
        self,
        path: Optional[str] = None,
        *,
        allow_secret_tainted: bool = False,
    ) -> bytes:
        """Capture a screenshot of the current page.

        Args:
            path: Optional filesystem path to also write the PNG to.
            allow_secret_tainted: Permit a screenshot after a secret fill.

        Returns:
            The PNG image bytes.
        """
        if self.secret_tainted and not allow_secret_tainted:
            raise SecretPolicyError(
                "Screenshots are blocked after a secret fill. "
                "Pass allow_secret_tainted=True to accept the risk."
            )
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

    @property
    def secret_tainted(self) -> bool:
        """Return true after a secret reaches the browser session."""
        return self._secret_state.tainted

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
        """Build a Playwright locator for the snapshot ``index``, self-healing.

        The selector targets the ``data-wraith-index`` attribute stamped onto
        the live DOM by the most recent :func:`wraith.snapshot.take_snapshot`.
        If that stamp is gone (the DOM changed since the snapshot), re-resolve by
        the element's content :pyattr:`~wraith.snapshot.Element.signature`:
        re-snapshot once and return the locator for the element that still
        matches — so a slightly-stale index recovers instead of throwing.
        """
        loc = self.page.locator(f'[data-wraith-index="{int(index)}"]')
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            return loc
        # Stale stamp — try to heal by signature against a fresh snapshot.
        prior = self.last_snapshot.by_index(index) if self.last_snapshot else None
        if prior is not None:
            sig = prior.signature
            fresh = self.snapshot()
            match = next((e for e in fresh.elements if e.signature == sig), None)
            if match is not None:
                return self.page.locator(f'[data-wraith-index="{int(match.index)}"]')
        return loc  # let the caller's action raise a clear error if truly gone

    def _read_field_value(self, locator: Any) -> Optional[str]:
        """Best-effort read of ``locator``'s current text, for clear-verification.

        ``input_value()`` is authoritative for ``<input>``/``<textarea>``/
        ``<select>`` — including a hidden ``<textarea>`` a virtualized editor
        like CodeMirror renders over, which is still a real textarea node — so
        it's tried first. It raises on element kinds it doesn't apply to (e.g.
        a ``contenteditable`` div), so we fall back to the rendered text.
        Neither path requires the pointer-actionability checks ``click()``
        does, so this is safe to call even when an overlay would make a click
        time out.
        """
        with contextlib.suppress(Exception):
            return locator.input_value()
        with contextlib.suppress(Exception):
            return locator.inner_text()
        with contextlib.suppress(Exception):
            return locator.text_content()
        return None

    def _is_effectively_empty(self, locator: Any) -> Optional[bool]:
        """Whether ``locator`` currently reads as empty; ``None`` if unknown."""
        value = self._read_field_value(locator)
        if value is None:
            return None
        return value.strip() == ""

    def _clear_verified(self, locator: Any) -> None:
        """Clear ``locator``'s content and verify it actually emptied.

        See :meth:`type` for the full rationale. Two attempts, each checked
        against a read-back rather than trusting "no exception":

        1. ``locator.fill("")`` — fast path, correct for plain form fields.
        2. A keyboard-driven select-all + delete (``focus()`` then
           ``ControlOrMeta+a``, ``Backspace``, ``Delete``) — real key events
           that a JS-owned document model (CodeMirror and similar) processes
           correctly. Uses ``focus()``, not ``click()``, since a virtualized
           editor's overlay can intercept pointer events and make ``click()``
           time out even though the underlying node is perfectly focusable.

        Raises:
            ClearFailedError: the field still reads non-empty (or unreadable)
                after both attempts.
        """
        with contextlib.suppress(Exception):
            locator.fill("")
        if self._is_effectively_empty(locator):
            return

        with contextlib.suppress(Exception):
            locator.focus()
        with contextlib.suppress(Exception):
            locator.press("ControlOrMeta+a")
        with contextlib.suppress(Exception):
            locator.press("Backspace")
        with contextlib.suppress(Exception):
            locator.press("Delete")

        if self._is_effectively_empty(locator):
            return

        raise ClearFailedError(
            "could not verify the field was cleared before typing — "
            "fill('') and a keyboard select-all+delete both left it "
            "non-empty (or its content could not be read back); refusing to "
            "type on top of unknown existing content"
        )

    @property
    def _secret_state(self) -> _SecretSessionState:
        """Return policy state stored on the shared browser context."""
        context = self.context
        state = getattr(context, "_wraith_secret_state", None)
        if isinstance(state, _SecretSessionState):
            return state
        state = _SecretSessionState()
        try:
            setattr(context, "_wraith_secret_state", state)
        except Exception as exc:
            raise SecretPolicyError(
                "The browser context cannot store secret policy state"
            ) from exc
        return state

    @staticmethod
    def _secret_field_metadata(locator: Any) -> dict[str, str]:
        """Read field semantics without reading its value."""
        try:
            raw = locator.evaluate(
                """element => ({
                    tag: (element.tagName || '').toLowerCase(),
                    type: (element.type || element.getAttribute('type') || '').toLowerCase(),
                    autocomplete: (element.getAttribute('autocomplete') || '').toLowerCase(),
                    contenteditable: element.isContentEditable === true,
                    disabled: element.disabled === true,
                    readonly: element.readOnly === true,
                })"""
            )
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "tag": str(raw.get("tag") or "").lower(),
            "type": str(raw.get("type") or "").lower(),
            "autocomplete": str(raw.get("autocomplete") or "").lower(),
            "contenteditable": "true" if raw.get("contenteditable") else "false",
            "disabled": "true" if raw.get("disabled") else "false",
            "readonly": "true" if raw.get("readonly") else "false",
        }

    @staticmethod
    def _secret_field_matches(kind: str, metadata: Mapping[str, str]) -> bool:
        """Match a declared secret kind to trusted DOM field semantics."""
        tag = metadata.get("tag", "")
        input_type = metadata.get("type", "") or "text"
        autocomplete = set(metadata.get("autocomplete", "").split())
        autocomplete_by_kind = {
            "password": {"current-password", "new-password"},
            "username": {"username"},
            "email": {"email"},
            "otp": {"one-time-code"},
            "card-number": {"cc-number"},
            "card-expiry": {"cc-exp", "cc-exp-month", "cc-exp-year"},
            "card-cvc": {"cc-csc"},
            "text": set(),
        }
        known_autocomplete = autocomplete & set().union(*autocomplete_by_kind.values())
        allowed_autocomplete = autocomplete_by_kind.get(kind, set())

        if tag not in {"input", "textarea"}:
            return False
        if metadata.get("disabled") == "true" or metadata.get("readonly") == "true":
            return False
        if known_autocomplete and (
            not allowed_autocomplete
            or not known_autocomplete.issubset(allowed_autocomplete)
        ):
            return False
        if tag == "textarea":
            return kind == "text" and not known_autocomplete
        fillable_input_types = {
            "color",
            "date",
            "time",
            "datetime-local",
            "month",
            "range",
            "week",
            "email",
            "number",
            "password",
            "search",
            "tel",
            "text",
            "url",
        }
        if input_type not in fillable_input_types:
            return False
        if kind == "password":
            return (
                input_type == "password"
                or bool(autocomplete & {"current-password", "new-password"})
            )
        if kind == "username":
            return "username" in autocomplete and input_type in {"text", "email"}
        if kind == "email":
            return input_type == "email" or "email" in autocomplete
        if kind == "otp":
            return "one-time-code" in autocomplete and input_type in {
                "text",
                "tel",
                "number",
            }
        if kind == "card-number":
            return input_type in {"text", "tel", "number"} and "cc-number" in autocomplete
        if kind == "card-expiry":
            return input_type in {"text", "tel", "number", "month"} and bool(
                autocomplete & {"cc-exp", "cc-exp-month", "cc-exp-year"}
            )
        if kind == "card-cvc":
            return input_type in {"text", "tel", "number", "password"} and "cc-csc" in autocomplete
        if kind == "text":
            return input_type == "text" and not known_autocomplete
        return False

    def _ensure_high_score(self) -> Optional[Any]:
        """Best-effort reCAPTCHA-v3 score lift via the reputation source.

        No-op when no ``reputation`` source was configured. Otherwise delegates
        to :func:`wraith.recaptcha_v3.ensure_high_score`, which detects the
        page's reCAPTCHA params, primes the context with the reputation cookies,
        and (best-effort) verifies a ``/recaptcha/api2/reload`` request carried
        them. The function is idempotent/cached per (context, host), so calling
        it on every :meth:`navigate` is cheap. Never raises — a missing
        ``recaptcha_v3`` module or a probe failure must not break navigation.

        Returns:
            The :class:`wraith.recaptcha_v3.RecaptchaParams` produced by
            ``ensure_high_score``, or ``None`` if no source is set or the lift
            could not run.
        """
        if self.reputation is None:
            return None
        try:
            from .recaptcha_v3 import ensure_high_score

            return ensure_high_score(self.page, source=self.reputation)
        except Exception:
            # recaptcha_v3 unavailable, or the probe/injection failed: the score
            # lift is opportunistic, so we swallow and let navigation proceed.
            return None

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
    reputation: Optional[Any] = None,
    secret_providers: Optional[Mapping[str, SecretProvider]] = None,
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
        reputation: Optional :class:`wraith.recaptcha_v3.ReputationSource`;
            forwarded to :class:`AgentBrowser` (un-partition launch prefs +
            ``ensure_high_score`` on navigate).
        secret_providers: Optional local secret providers by name.
        **launch_kw: Forwarded to :func:`wraith.engine.launch` when
            self-launching.

    Example::

        with agent_browser(engine="camoufox", headless=True) as ab:
            print(ab.navigate("https://example.com").to_text())
    """
    ab = AgentBrowser(
        session=session,
        engine=engine,
        reputation=reputation,
        secret_providers=secret_providers,
        **launch_kw,
    )
    try:
        yield ab
    finally:
        ab.close()
