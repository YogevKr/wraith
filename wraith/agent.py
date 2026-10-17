"""wraith.agent — a browser-use-style perception/action layer over the stealth engine.

This module turns Wraith's stealth :class:`~wraith.engine.Session` into an
*agent-friendly* surface: an LLM (or any caller) drives the browser by reading a
flat, indexed :class:`~wraith.snapshot.Snapshot` of the interactive elements on
the page and then acting on those elements **by integer index** — ``click(12)``,
``type(7, "hello")`` — instead of hand-crafting CSS/XPath selectors.

The indexing contract is owned by :mod:`wraith.snapshot`:
:func:`~wraith.snapshot.take_snapshot` walks the DOM, finds the interactive
elements, assigns each a sequential integer index, and keeps index -> node in
Wraith's isolated JavaScript world, where the page cannot see it. This class
resolves an index to that node (:data:`wraith.snapshot._INDEXED_NODE_JS`) — so
a snapshot must be reasonably fresh for an index to resolve (every mutating
action re-snapshots so the indices the caller sees are always current).

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
from typing import Any, Callable, Mapping, Optional

from . import engine as _engine
from .behavior import click_hold_ms, key_hold_ms
from .identity import export_storage_state
from .domains import DomainPolicy
from .netlog import NetworkLog
from .uploads import UploadPolicy, require_policy
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
    get_vault_provider,
    SECRET_FIELD_KINDS,
)
from .redaction import redacted_exception
from .snapshot import Snapshot, take_snapshot, _INDEXED_NODE_JS, _POINTER_POINT_JS

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


_NAVIGATION_START_MS = 500
_FRAME_CLICK_TIMEOUT_MS = 3000
_NAVIGATION_COMMIT_MS = 10000
_HAS_FOCUS_JS = "t => t === document.activeElement || t.contains(document.activeElement)"
_EDITABLE_JS = """el => {
  if (el.isContentEditable) return true;
  const tag = el.tagName.toLowerCase();
  if (tag === 'textarea') return !el.disabled && !el.readOnly;
  if (tag === 'input') {
    const kind = (el.type || 'text').toLowerCase();
    const noText = ['checkbox', 'radio', 'button', 'submit', 'reset', 'image',
                    'file', 'hidden', 'range', 'color'];
    return !noText.includes(kind) && !el.disabled && !el.readOnly;
  }
  const role = (el.getAttribute('role') || '').toLowerCase();
  return ['textbox', 'searchbox', 'combobox', 'spinbutton'].includes(role);
}"""
_FOCUSED_VALUE_JS = """() => {
  const a = document.activeElement;
  if (!a || a === document.body) return null;
  if (typeof a.value === 'string') return a.value;
  return a.isContentEditable ? a.innerText : null;
}"""


_SELECT_TIMEOUT_MS = 3000
_FILE_CHOOSER_TIMEOUT_MS = 5000
_WAIT_MAX_SECONDS = 60.0

# [value, label] for each option of a <select>, or null for another element.
_SELECT_OPTIONS_JS = """el => el.tagName === 'SELECT'
    ? Array.from(el.options).map(o => [o.value, o.label.trim()])
    : null"""

_TEXT_SHOWN_JS = "text => !!document.body && document.body.innerText.includes(text)"


class _IndexedNode:
    """One snapshot node, used where a Playwright locator would be.

    It wraps the ``ElementHandle`` from the snapshot's index Map; ``count()``
    is 0 when the node is gone. Other attributes come from the handle.
    """

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    @classmethod
    def resolve(cls, scope: Any, index: int) -> "_IndexedNode":
        """Look ``index`` up in ``scope`` (a page or frame)."""
        try:
            return cls(scope.evaluate_handle(_INDEXED_NODE_JS, int(index)).as_element())
        except Exception:
            return cls(None)

    def count(self) -> int:
        return 0 if self._handle is None else 1

    def element_handle(self) -> Any:
        return self._handle

    def __getattr__(self, name: str) -> Any:
        if self._handle is None:
            raise RuntimeError("The indexed element is no longer on the page")
        return getattr(self._handle, name)


class _FocusedKeyboard:
    """Send key events to whichever control has focus."""

    def __init__(self, keyboard: Any) -> None:
        self._keyboard = keyboard

    def press_sequentially(self, text: str, delay: float = 0) -> None:
        self._keyboard.type(text, delay=delay)

    def press(self, key: str, delay: float = 0) -> None:
        self._keyboard.press(key, delay=delay)


class ClearFailedError(RuntimeError):
    """:meth:`AgentBrowser.type` could not verify a field was actually cleared.

    Raised instead of silently typing on top of unknown existing content. See
    :meth:`AgentBrowser.type` for the full explanation: some editors (notably
    CodeMirror, whose visible content lives in a JS-owned document model
    behind a hidden ``<textarea>``) let ``locator.fill("")`` complete with no
    exception while leaving the real document untouched, so "no exception" is
    not sufficient signal that a field is actually empty.
    """


def _has_focus(target: Any) -> bool:
    """Whether keyboard focus is on ``target`` or inside it."""
    with contextlib.suppress(Exception):
        return bool(target.evaluate(_HAS_FOCUS_JS))
    return False


def _focused_value(scope: Any) -> Optional[str]:
    """The focused control's text in ``scope`` (a page or frame), or ``None``."""
    with contextlib.suppress(Exception):
        value = scope.evaluate(_FOCUSED_VALUE_JS)
        return value if isinstance(value, str) else None
    return None


def _clear_focused(page: Any, frame: Any = None) -> None:
    """Clear the focused control with keys, then verify it is empty.

    ``frame`` is the frame that holds the field; its document knows the
    focused control (the main document only sees the iframe).
    """
    scope = frame if frame is not None else page
    value = _focused_value(scope)
    if value is not None and value.strip() == "":
        return
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.press("Backspace")
    value = _focused_value(scope)
    if value is not None and value.strip() == "":
        return
    raise ClearFailedError(
        "could not verify the focused field was cleared before typing — "
        "a keyboard select-all+delete left it non-empty (or its content "
        "could not be read back); refusing to type on top of unknown "
        "existing content"
    )


def _click_in_frame(target: Any) -> None:
    """Click a node inside a child frame once.

    Playwright maps the point through the iframe (and its CSS transforms) and
    checks that the node receives the pointer before it dispatches; it never
    repeats a dispatched click.
    """
    if target is None:
        raise RuntimeError("The visible target is unavailable")
    target.click(timeout=_FRAME_CLICK_TIMEOUT_MS, delay=click_hold_ms())


def _human_click(page: Any, aim: Callable[[], tuple[float, float]]) -> None:
    """Move along a human-like path to ``aim()``, re-aim, then click once.

    The second ``aim()`` follows a target that moved during the travel.
    """
    with contextlib.suppress(Exception):
        from .behavior import human_move

        human_move(page, target=aim())
    x, y = aim()
    page.mouse.click(x, y, delay=click_hold_ms())


def _frame_aim(
    snap: Optional[Snapshot], frame: int, x: float, y: float
) -> Callable[[], tuple[float, float]]:
    """Return a function that maps frame fractions to a point at call time."""
    fx, fy = float(x), float(y)
    if not (0 <= fx <= 1 and 0 <= fy <= 1):
        raise ValueError("click_frame needs x and y between 0 and 1")
    # A snapshot with no indexed elements is falsy (len 0) but can still list
    # frames, so test for None.
    handle = snap.frame_handle(int(frame)) if snap is not None else None
    if handle is None:
        raise ValueError(f"frame {frame} is not in the last snapshot")
    iframe = handle.frame_element()

    def aim() -> tuple[float, float]:
        box = iframe.bounding_box()
        if not box:
            raise RuntimeError(f"frame {frame} is no longer visible")
        return box["x"] + fx * box["width"], box["y"] + fy * box["height"]

    return aim


def _await_navigation(
    page: Any, action: Callable[[], Any], allows: Optional[Callable[[str], bool]] = None
) -> None:
    """Run ``action``; when it starts a page navigation, wait for the commit.

    A click or a form submit sends its request at once, but the new page
    commits later, so a snapshot taken right away shows the old page. An
    action that starts no navigation costs at most ``_NAVIGATION_START_MS``.
    Errors from ``action`` propagate; a wait that times out does not.
    """
    if not hasattr(page, "expect_event"):
        action()
        return
    main = getattr(page, "main_frame", None)
    error: Optional[BaseException] = None

    def _run() -> None:
        nonlocal error
        try:
            action()
        except Exception as exc:
            error = exc

    with contextlib.suppress(Exception):
        with page.expect_event(
            "framenavigated",
            predicate=lambda frame: frame == main,
            timeout=_NAVIGATION_COMMIT_MS,
        ):
            with page.expect_request(
                lambda request: request.is_navigation_request()
                and request.frame == main
                and (allows is None or allows(request.url)),
                timeout=_NAVIGATION_START_MS,
            ):
                _run()
    if error is not None:
        raise error


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
        allowed_domains: Any = None,
        upload_dirs: Any = None,
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
            allowed_domains: Optional host allowlist (``"a.com,*.b.com"``, a
                list, or a :class:`~wraith.domains.DomainPolicy`). The browser
                then refuses requests, WebSockets, and navigations to other
                hosts, and closes a page that a redirect moved off the list.
            upload_dirs: Folders :meth:`upload_file` may read from (a list,
                ``os.pathsep``-joined text, or an
                :class:`~wraith.uploads.UploadPolicy`). ``None`` (default)
                turns file upload off.
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
        self.domains: Optional[DomainPolicy] = DomainPolicy.parse(allowed_domains)
        self._domains_installed: bool = False
        self.uploads: Optional[UploadPolicy] = UploadPolicy.parse(upload_dirs)
        self.network_log = NetworkLog()
        self._network_installed: bool = False

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
        if self.domains is not None and not self._domains_installed:
            self.domains.install(self._session.context)
            self._domains_installed = True
        if not self._network_installed:
            self._network_installed = True
            # A session object without Playwright events has no network log.
            with contextlib.suppress(AttributeError):
                self.network_log.install(self._session.context)
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
        if url and self.domains is not None:
            self.domains.check(url)
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
        for_chromium: bool = False,
    ) -> str:
        """Export the context's cookies + localStorage to a Playwright
        ``storageState`` JSON file — a portable, reusable authenticated session
        (the durable form of an identity-borrowed / challenge-cleared context).

        Pass ``for_chromium=True`` when a Chromium tool such as agent-browser
        will load the file (see :func:`wraith.identity.chromium_storage_state`).
        """
        self._refuse_after_secret("Storage export is", allow_secret_tainted)
        return export_storage_state(self.context, path, for_chromium=for_chromium)

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
        if kw.get("highlight"):
            self._refuse_after_secret("Highlighted snapshots are", allow_secret_tainted)
        self._enforce_location()
        if self.domains is not None:
            kw.setdefault("frame_filter", self.domains.allows)
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
        if self.domains is not None:
            self.domains.check(url)
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
    # Actions (all act by snapshot index and re-snapshot)
    # ------------------------------------------------------------------ #
    def click(self, index: int) -> Snapshot:
        """Click the element with the given snapshot ``index``.

        Pins the indexed element, checks actionability, and clicks its current
        visible point. Legacy snapshots without coordinates use locator clicks.

        Args:
            index: The integer index from the current snapshot.

        Returns:
            A fresh :class:`~wraith.snapshot.Snapshot` taken after the click
            settles.
        """
        return self._act(lambda: self._pointer_click(index))

    def click_at(self, x: float, y: float) -> Snapshot:
        """Click the main-viewport point ``(x, y)`` in CSS pixels.

        Use it for a frame whose content Wraith cannot index, such as a closed
        shadow root (Cloudflare Turnstile): aim with the frame box in the
        snapshot and :meth:`screenshot` (``scale="css"`` matches these units).
        The mouse travels a human-like path first. Wraith cannot check what is
        under the point, so prefer :meth:`click` when the target has an index.
        """
        x, y = float(x), float(y)
        if not (x >= 0 and y >= 0):  # also rejects NaN
            raise ValueError("click_at needs a point inside the viewport")
        return self._act(lambda: _human_click(self.page, lambda: (x, y)))

    def click_frame(self, frame: int, x: float, y: float) -> Snapshot:
        """Click a point of child frame ``frame`` from the last snapshot.

        ``x`` and ``y`` are fractions (0 to 1) of the frame's on-screen box,
        so a checkbox at the left middle is ``(0.1, 0.5)``. Wraith reads the
        box at click time, and again after the mouse travel, so a frame that
        moves (a CSS animation) is still hit. Use it for a frame with no
        indexed elements, such as Cloudflare Turnstile.
        """
        aim = _frame_aim(self.last_snapshot, frame, x, y)
        return self._act(lambda: _human_click(self.page, aim))

    def _act(self, action: Callable[[], Any]) -> Snapshot:
        """Run ``action``, wait for a navigation it starts, and re-snapshot."""
        mark = self._blocked_mark()
        pre = self._page_signature()
        _await_navigation(self.page, action, self._allows)
        self._wait_for_settle()
        snap = self.snapshot()
        self._set_changed(pre, snap)
        self._raise_if_blocked(mark)
        return snap

    @property
    def _allows(self) -> Optional[Callable[[str], bool]]:
        return self.domains.allows if self.domains is not None else None

    def _blocked_mark(self) -> int:
        return self.domains.blocked_navigations if self.domains is not None else 0

    def _raise_if_blocked(self, mark: int) -> None:
        """Report a navigation that the allowlist refused during an action."""
        if self.domains is not None:
            self.domains.raise_if_blocked(mark)

    def _enforce_location(self) -> None:
        """Close a page that a server redirect moved off the allowlist."""
        if self.domains is not None and self._session is not None:
            self.domains.close_if_outside(self.page)

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
        # A stale or wrong index must not click a link or a buy button.
        self._require_editable(index)
        mark = self._blocked_mark()
        pre = self._page_signature()
        # The pointer selects the visible target. The locator then sends keys
        # to the focused control without trusting hidden DOM candidates.
        locator = None
        try:
            locator = self._pointer_focus(index)
        except Exception:
            pass
        if locator is None:
            raise RuntimeError("The browser could not focus the visible target")

        # Keys follow focus, as they do for a person. Some fields replace their
        # node on input (Wikipedia's search box), which detaches the pinned
        # handle while focus moves to the new node.
        focused = _has_focus(locator)
        if clear:
            if focused:
                _clear_focused(self.page, self._frame_of(index))
            else:
                self._clear_verified(locator)
        keys = _FocusedKeyboard(self.page.keyboard) if focused else locator

        typed = False
        # Prefer human-paced typing for reputation-sensitive fields; degrade to
        # fill() if behavior helpers or per-key typing aren't available.
        try:
            from .behavior import human_type

            human_type(keys, text, click=False)
            typed = True
        except Exception:
            typed = False
        if not typed:
            fill_error = None
            try:
                locator.fill(text)
            except Exception as exc:
                fill_error = redacted_exception(exc, (text,))
            if fill_error is not None:
                raise fill_error

        if enter:
            with contextlib.suppress(Exception):
                _await_navigation(self.page, lambda: keys.press("Enter"), self._allows)

        self._wait_for_settle()
        snap = self.snapshot()
        self._set_changed(pre, snap)
        self._raise_if_blocked(mark)
        return snap

    def fill_vault_item(
        self,
        index: int,
        item_id: str,
        *,
        field_kind: str,
        provider: str = "vault",
    ) -> bool:
        """Fill one visible field from a vault item and return only a boolean.

        The registered vault provider receives the item ID and trusted field
        context. Wraith returns no field value or snapshot and emits no fill logs.
        """
        if not isinstance(item_id, str) or not item_id.strip():
            raise SecretCapabilityError("The vault item ID is required")
        kind = str(field_kind).strip().lower().replace("_", "-")
        if kind not in SECRET_FIELD_KINDS:
            raise SecretCapabilityError("The vault field kind is invalid")

        self._require_main_frame(index)
        origin = canonical_origin(self.current_url)
        locator = self._locator(index)
        try:
            element = locator.element_handle()
        except Exception:
            element = None
        if element is None:
            raise SecretPolicyError("The target field is not available")
        metadata = self._secret_field_metadata(element)
        if not self._secret_field_matches(kind, metadata):
            raise SecretPolicyError("The target field does not match field_kind")
        context = SecretRequestContext(
            origin=origin,
            frame_origin=origin,
            field_kind=kind,
            field_tag=metadata["tag"],
            field_type=metadata["type"],
            autocomplete=metadata["autocomplete"],
            index=int(index),
        )
        vault = get_vault_provider(provider)
        authorized = False
        authorization_failed = False
        try:
            authorized = vault.authorize_item(item_id.strip(), origin) is True
        except Exception:
            authorization_failed = True
        if authorization_failed:
            raise SecretProviderError("The vault provider failed")
        if not authorized:
            raise SecretPolicyError("The vault item is not allowed for this origin")

        material = None
        resolution_failed = False
        try:
            material = vault.resolve_item(item_id.strip(), context)
        except Exception:
            resolution_failed = True
        if resolution_failed:
            raise SecretProviderError("The vault provider failed")
        if not isinstance(material, SecretMaterial):
            raise SecretProviderError("The vault provider returned invalid material")

        browser_failed = False
        try:
            if canonical_origin(self.current_url) != origin:
                raise SecretPolicyError("The current origin changed during vault use")
            self._click_pointer_target(element)
            if canonical_origin(self.current_url) != origin:
                raise SecretPolicyError("The current origin changed during vault use")
            current_metadata = self._secret_field_metadata(element)
            if not self._secret_field_matches(kind, current_metadata):
                raise SecretPolicyError("The target field changed during vault use")
            if not element.evaluate(_POINTER_POINT_JS):
                raise SecretPolicyError("The target field is covered or unavailable")
            self._secret_state.tainted = True
            element.fill(material.reveal())
        except SecretPolicyError:
            raise
        except Exception:
            browser_failed = True
        finally:
            material.clear()
        if browser_failed:
            raise SecretProviderError("The browser could not fill the vault item")
        self._wait_for_settle()
        return True

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

        self._require_main_frame(index)
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

    def press_key(self, key: str) -> Snapshot:
        """Press one key or chord on the focused element and re-snapshot.

        Use it for keys that :meth:`type` does not send: ``"Escape"`` (close a
        dialog), ``"Tab"`` / ``"Shift+Tab"``, ``"ArrowDown"``, ``"PageDown"``,
        ``"Enter"``. Names follow Playwright's ``keyboard.press``. The key is
        held for a human time; a navigation it starts is awaited.
        """
        name = str(key or "").strip()
        if not name:
            raise ValueError("press_key needs a key name, such as Escape or ArrowDown")
        return self._act(lambda: self.page.keyboard.press(name, delay=key_hold_ms()))

    def select_option(self, index: int, option: str) -> Snapshot:
        """Choose ``option`` (its value or its visible label) in a ``<select>``.

        Raises when ``index`` is not a ``<select>`` or no option matches; the
        error lists the options.
        """
        return self._act(lambda: self._select(index, str(option)))

    def _select(self, index: int, option: str) -> None:
        handle = self._locator(index).element_handle()
        if handle is None:
            raise RuntimeError("The indexed element is no longer on the page")
        labels = handle.evaluate(_SELECT_OPTIONS_JS)
        if labels is None:
            raise RuntimeError(f"[{index}] is not a <select> list")
        if not any(option in pair for pair in labels):
            choices = ", ".join(label or value for value, label in labels[:30])
            raise ValueError(f"No option {option!r} in [{index}]. Options: {choices}")
        handle.select_option(option, timeout=_SELECT_TIMEOUT_MS)

    def upload_file(self, index: int, paths: Any) -> Snapshot:
        """Put local files into the file input or upload button at ``index``.

        ``paths`` is one path or a list. Each file must be inside a folder
        allowed with ``upload_dirs``; uploads are off without one. A file
        ``<input>`` takes the files directly; any other element is clicked
        and the file chooser it opens gets the files.
        """
        files = require_policy(self.uploads).check(paths)
        return self._act(lambda: self._upload(index, files))

    def _upload(self, index: int, files: list[str]) -> None:
        handle = self._locator(index).element_handle()
        if handle is None:
            raise RuntimeError("The indexed element is no longer on the page")
        if handle.evaluate("el => el.tagName === 'INPUT' && el.type === 'file'"):
            handle.set_input_files(files)
            return
        with self.page.expect_file_chooser(timeout=_FILE_CHOOSER_TIMEOUT_MS) as chooser:
            self._pointer_click(index)
        chooser.value.set_files(files)

    def go_back(self) -> Snapshot:
        """Go back one page in the tab's history and re-snapshot."""
        return self._act(lambda: self.page.go_back(wait_until="commit"))

    def go_forward(self) -> Snapshot:
        """Go forward one page in the tab's history and re-snapshot."""
        return self._act(lambda: self.page.go_forward(wait_until="commit"))

    def wait_for(self, text: Optional[str] = None, seconds: float = 5.0) -> Snapshot:
        """Wait until ``text`` shows in the page, or for ``seconds``, then re-snapshot.

        With ``text``, raise :class:`TimeoutError` when it does not appear in
        the main frame's visible text within ``seconds`` (at most 60).
        """
        limit = min(max(float(seconds), 0.0), _WAIT_MAX_SECONDS)
        if text:
            try:
                self.page.wait_for_function(
                    _TEXT_SHOWN_JS, arg=str(text), timeout=limit * 1000
                )
            except Exception as exc:
                raise TimeoutError(
                    f"{text!r} did not appear within {limit:g} s"
                ) from exc
        else:
            self.page.wait_for_timeout(limit * 1000)
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
        self._enforce_location()
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
        self._enforce_location()
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
        scale: str = "device",
    ) -> bytes:
        """Capture a screenshot of the current page.

        Args:
            path: Optional filesystem path to also write the PNG to.
            allow_secret_tainted: Permit a screenshot after a secret fill.
            scale: ``"device"`` (full resolution) or ``"css"`` (one image
                pixel per CSS pixel, the unit of :meth:`click_at`).

        Returns:
            The PNG image bytes.
        """
        self._enforce_location()
        self._refuse_after_secret("Screenshots are", allow_secret_tainted)
        if path is not None:
            return self.page.screenshot(path=path, scale=scale)
        return self.page.screenshot(scale=scale)

    def network(
        self, contains: str = "", resource_type: str = "", limit: int = 50
    ) -> list[dict]:
        """The last ``limit`` requests whose URL contains ``contains``.

        ``resource_type`` filters by Playwright's type (``xhr``, ``fetch``,
        ``document``, ``script``, ``image``...). Each dict has ``id``,
        ``method``, ``url``, ``type``, ``status``, ``content_type`` and
        ``failure``; pass the ``id`` to :meth:`network_response`. After a
        secret fill, URLs come without their query strings.
        """
        self.session  # the log starts with the session
        entries = self.network_log.entries(
            contains=contains, resource_type=resource_type, limit=limit
        )
        return [entry.to_dict(strip_query=self.secret_tainted) for entry in entries]

    def network_response(
        self, request_id: int, *, allow_secret_tainted: bool = False
    ) -> str:
        """The response body of request ``request_id`` from :meth:`network`.

        Text bodies (HTML, JSON, JavaScript, XML) come back decoded; binary
        bodies as a one-line note. Blocked after a secret fill, because a
        response can echo the secret or a session token.
        """
        self._refuse_after_secret("Response bodies are", allow_secret_tainted)
        return self.network_log.body(request_id)

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

    def _refuse_after_secret(self, what: str, allow: bool) -> None:
        """Raise when output that can expose a filled secret is requested."""
        if self.secret_tainted and not allow:
            raise SecretPolicyError(
                f"{what} blocked after a secret fill. "
                "Pass allow_secret_tainted=True to accept the risk."
            )

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
    def _locator(self, index: int) -> "_IndexedNode":
        """Resolve the snapshot ``index`` to its live node, self-healing.

        The node comes from the index Map the most recent
        :func:`wraith.snapshot.take_snapshot` kept in Wraith's isolated world.
        If the node is gone (the DOM changed since the snapshot), re-resolve by
        the element's content :pyattr:`~wraith.snapshot.Element.signature`:
        re-snapshot once and return the element that still matches — so a
        slightly-stale index recovers instead of throwing.
        """
        node = _IndexedNode.resolve(self._frame_of(index) or self.page, index)
        if node.count():
            return node
        prior = self.last_snapshot.by_index(index) if self.last_snapshot else None
        if prior is not None:
            sig = prior.signature
            fresh = self.snapshot()
            match = next((e for e in fresh.elements if e.signature == sig), None)
            if match is not None:
                scope = self._frame_of(match.index) or self.page
                return _IndexedNode.resolve(scope, match.index)
        return node  # let the caller's action raise a clear error if truly gone

    def _frame_of(self, index: int) -> Any:
        """The child ``Frame`` that holds ``index``, or ``None`` for the main frame."""
        snap = self.last_snapshot
        element = snap.by_index(index) if snap else None
        if element is None or not element.frame:
            return None
        return snap.frame_handle(element.frame)

    def _require_main_frame(self, index: int) -> None:
        """Secret fills bind to the page origin, so frame fields never qualify."""
        if self._frame_of(index) is not None:
            raise SecretPolicyError("Secret fills accept only main-frame fields")

    def _pointer_click(self, index: int) -> None:
        """Pin the indexed node, then click its current visible point once."""
        failed = False
        try:
            element = self.last_snapshot.by_index(index) if self.last_snapshot else None
            locator = self._locator(index)
            if element is not None and element.frame:
                _click_in_frame(locator.element_handle())
            elif element is None or element.center is None:
                locator.click(delay=click_hold_ms())
            else:
                self._click_pointer_target(locator.element_handle())
        except Exception:
            failed = True
        if failed:
            raise RuntimeError("The browser could not click the visible target")

    def _click_pointer_target(self, target: Any) -> None:
        """Check actionability on a pinned node, then send one pointer click."""
        if target is None:
            raise RuntimeError("The visible target is unavailable")
        target.scroll_into_view_if_needed()
        # Trial mode checks enabled/stable state without dispatching input.
        target.click(trial=True, timeout=1500)
        point = target.evaluate(_POINTER_POINT_JS)
        if not point:
            raise RuntimeError("The visible target is covered or unavailable")
        # Never retry a dispatched click: it may have submitted a purchase.
        self.page.mouse.click(point["x"], point["y"], delay=click_hold_ms())

    def _pointer_focus(self, index: int) -> Any:
        """Return the pinned field after focusing its current visible point."""
        prior = self.last_snapshot.by_index(index) if self.last_snapshot else None
        locator = self._locator(index)
        if prior is not None and prior.frame:
            target = locator.element_handle()
            _click_in_frame(target)
            return target
        if prior is not None and prior.center is not None:
            target = locator.element_handle()
            self._click_pointer_target(target)
            return target
        else:
            focus = getattr(locator, "focus", None)
            if callable(focus):
                focus()
        # Legacy snapshots also support keyboard-only virtual editor inputs.
        return locator

    def _require_editable(self, index: int) -> None:
        """Refuse to type into an element that does not take text."""
        try:
            editable = self._locator(index).evaluate(_EDITABLE_JS)
        except Exception:
            return  # unknown: the click and clear checks still apply
        if editable is False:
            el = self.last_snapshot.by_index(index) if self.last_snapshot else None
            kind = f"<{el.tag} role={el.role or '-'}>" if el is not None else "element"
            raise RuntimeError(
                f"[{index}] is a {kind}, not a text field. Take a fresh "
                "snapshot and use the index of the field."
            )

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
    allowed_domains: Any = None,
    upload_dirs: Any = None,
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
        allowed_domains: Optional host allowlist; see :class:`AgentBrowser`.
        upload_dirs: Optional upload folders; see :class:`AgentBrowser`.
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
        allowed_domains=allowed_domains,
        upload_dirs=upload_dirs,
        **launch_kw,
    )
    try:
        yield ab
    finally:
        ab.close()
