"""Live session capture (token harvesting).

Many authentication bearer tokens are *not* cookies: they are minted
per-session and sent in an ``Authorization`` request header. Identity
borrowing via on-disk cookie extraction (see :mod:`wraith.identity`) cannot
reach those. This module closes the gap by watching live network traffic and
capturing the first request to a target API that carries both an
``Authorization`` header *and* the chosen authentication cookie, then
persisting ``{Authorization, Cookie, User-Agent}`` as a reusable session file.

The :class:`SessionHarvester` is engine-agnostic: it only needs a Playwright
(or patchright / camoufox) ``BrowserContext``. The high-level
:func:`harvest_session` helper launches a stealth engine for you and is what
the CLI drives; it imports :mod:`wraith.engine` lazily so this module still
loads on a partial install.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union
from urllib.parse import urlparse

__all__ = ["CapturedSession", "SessionHarvester", "harvest_session"]


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _header_lookup(headers: dict, name: str) -> Optional[str]:
    """Case-insensitive header lookup.

    Playwright's ``request.headers`` already lower-cases keys, but we stay
    defensive so the same logic works against an arbitrary header mapping.
    """
    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _cookie_value(cookie_header: Optional[str], cookie_name: str) -> Optional[str]:
    """Pull a single cookie's value out of a ``Cookie:`` header string."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        if key.strip() == cookie_name:
            return value.strip()
    return None


class CapturedSession:
    """A captured, reusable authenticated session."""

    def __init__(
        self,
        *,
        source: str,
        authorization: str,
        cookie: str,
        user_agent: str = "",
        saved_at: Optional[str] = None,
    ) -> None:
        self.source = source
        self.authorization = authorization
        self.cookie = cookie
        self.user_agent = user_agent
        self.saved_at = saved_at or _now_iso()

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "saved_at": self.saved_at,
            "headers": {
                "Authorization": self.authorization,
                "Cookie": self.cookie,
                "User-Agent": self.user_agent,
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CapturedSession":
        headers = data.get("headers", {}) or {}
        return cls(
            source=data.get("source", ""),
            authorization=headers.get("Authorization", ""),
            cookie=headers.get("Cookie", ""),
            user_agent=headers.get("User-Agent", ""),
            saved_at=data.get("saved_at"),
        )

    @classmethod
    def load(cls, path: Union[str, Path]) -> "CapturedSession":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"CapturedSession(source={self.source!r}, "
            f"authorization=<{len(self.authorization)} chars>, "
            f"cookie=<{len(self.cookie)} chars>)"
        )


class SessionHarvester:
    """Capture the first authenticated request to a target.

    Attach this to a Playwright ``BrowserContext`` and it will listen for
    network requests. It captures the first request whose URL matches the
    configured target (by substring and/or host) *and* that carries an
    ``Authorization`` header. If an ``auth_cookie`` name is supplied, the
    request must also carry that cookie before it is accepted — this avoids
    latching onto an unrelated bearer token (e.g. an analytics beacon).

    Usage::

        harvester = SessionHarvester(
            target_url="api.example.com/v1",
            auth_cookie="rbzid",
        )
        harvester.attach(context)
        page.goto("https://example.com/account")
        harvester.wait(timeout=30)
        harvester.save_session("session.json")
    """

    def __init__(
        self,
        target_url: Optional[str] = None,
        *,
        target_host: Optional[str] = None,
        auth_cookie: Optional[str] = None,
        auth_header: str = "Authorization",
        on_capture: Optional[Callable[[CapturedSession], None]] = None,
    ) -> None:
        if not target_url and not target_host:
            raise ValueError("provide at least one of target_url or target_host")
        self.target_url = target_url
        # Derive a host from the target URL when one was not given explicitly.
        self.target_host = (target_host or _host_of(target_url or "")) or None
        self.auth_cookie = auth_cookie
        self.auth_header = auth_header
        self._on_capture = on_capture

        self.captured: Optional[CapturedSession] = None
        self._context: Any = None
        self._listener: Optional[Callable[[Any], None]] = None

    # -- request matching ------------------------------------------------

    def _url_matches(self, url: str) -> bool:
        if self.target_url and self.target_url in url:
            return True
        if self.target_host and _host_of(url) == self.target_host:
            return True
        # Allow a bare host as target_url too (e.g. "api.example.com").
        if self.target_url and self.target_url == _host_of(url):
            return True
        return False

    def _consider(self, request: Any) -> None:
        """Inspect one request; latch the session if it qualifies."""
        if self.captured is not None:
            return
        try:
            url = request.url
        except Exception:
            return
        if not self._url_matches(url):
            return

        try:
            headers = request.headers
        except Exception:
            return

        authorization = _header_lookup(headers, self.auth_header)
        if not authorization:
            return

        cookie_header = _header_lookup(headers, "cookie") or ""
        if self.auth_cookie:
            # The chosen auth cookie must actually be present on this request.
            if _cookie_value(cookie_header, self.auth_cookie) is None:
                return

        user_agent = _header_lookup(headers, "user-agent") or ""

        self.captured = CapturedSession(
            source=url,
            authorization=authorization,
            cookie=cookie_header,
            user_agent=user_agent,
        )
        if self._on_capture is not None:
            try:
                self._on_capture(self.captured)
            except Exception:
                pass

    # -- lifecycle -------------------------------------------------------

    def attach(self, context: Any) -> "SessionHarvester":
        """Start listening on a Playwright ``BrowserContext``."""
        if self._listener is not None:
            raise RuntimeError("harvester already attached")
        self._context = context

        def _listener(request: Any) -> None:
            self._consider(request)

        self._listener = _listener
        context.on("request", _listener)
        return self

    def detach(self) -> None:
        """Stop listening (idempotent)."""
        if self._context is not None and self._listener is not None:
            try:
                self._context.remove_listener("request", self._listener)
            except Exception:
                pass
        self._context = None
        self._listener = None

    def wait(self, timeout: float = 60.0, poll: float = 0.25) -> Optional[CapturedSession]:
        """Block until a session is captured or ``timeout`` seconds elapse.

        Returns the :class:`CapturedSession` or ``None`` on timeout. This is a
        simple sleep-poll loop; the Playwright event loop runs on its own
        thread (sync API) so the captured value appears here as it fires.
        """
        deadline = time.monotonic() + timeout
        while self.captured is None and time.monotonic() < deadline:
            time.sleep(poll)
        return self.captured

    def save_session(self, path: Union[str, Path]) -> dict:
        """Persist the captured session as JSON and return the payload.

        Raises :class:`RuntimeError` if nothing has been captured yet.
        """
        if self.captured is None:
            raise RuntimeError(
                "no session captured yet — navigate the target as the "
                "authenticated user, then call wait() before save_session()"
            )
        payload = self.captured.to_dict()
        out = Path(path)
        if out.parent and not out.parent.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload


def harvest_session(
    target_url: str,
    out_path: Union[str, Path],
    *,
    url: Optional[str] = None,
    auth_cookie: Optional[str] = None,
    target_host: Optional[str] = None,
    auth_header: str = "Authorization",
    borrow_from: Optional[str] = None,
    engine: str = "camoufox",
    headless: bool = False,
    timeout: float = 120.0,
    geoip: bool = True,
    **engine_kwargs: Any,
) -> dict:
    """Launch a stealth browser, drive ``url`` and harvest a session.

    This is the high-level helper the CLI's ``harvest`` subcommand calls. It
    lazily imports :mod:`wraith.engine` (and :mod:`wraith.identity` when
    ``borrow_from`` is set) so :mod:`wraith.harvest` itself imports cleanly on
    a partial install.

    Parameters
    ----------
    target_url:
        URL/host substring that identifies the authenticated API request to
        capture (e.g. ``"api.example.com/v1"``).
    out_path:
        Where to write the session JSON.
    url:
        Page to open in the browser. Defaults to ``https://<target_host>/``
        derived from ``target_url``.
    auth_cookie:
        Name of the cookie that must accompany the bearer token.
    borrow_from:
        Optional real browser profile (path, or a directory found via
        :mod:`wraith.identity`) to seed cookies from before navigating.
    engine:
        ``"camoufox"`` (default/primary), ``"chromium"`` (patched-Chromium
        fallback) or ``"auto"``.
    """
    try:
        from wraith import engine as engine_mod  # lazy: partial installs
    except Exception as exc:  # pragma: no cover - depends on integrator module
        raise RuntimeError(
            "wraith.engine is not available; cannot launch a browser to "
            f"harvest. Underlying import error: {exc}"
        ) from exc

    host = target_host or _host_of(target_url)
    landing = url or (f"https://{host}/" if host else None)
    if not landing:
        raise RuntimeError(
            "could not determine a page URL to open: pass url= or a "
            "target_url with a host component"
        )

    harvester = SessionHarvester(
        target_url=target_url,
        target_host=target_host,
        auth_cookie=auth_cookie,
        auth_header=auth_header,
    )

    # engine.launch returns a Session (with .page/.context) that is also a
    # context manager. We still resolve loosely so a future engine that
    # yields a bare Browser/BrowserContext keeps working.
    with engine_mod.launch(
        engine,
        headless=headless,
        geoip=geoip,
        **engine_kwargs,
    ) as session:
        context = _resolve_context(session)
        page = _resolve_page(session, context)

        harvester.attach(context)

        if borrow_from:
            try:
                from wraith import identity as identity_mod  # lazy
            except Exception as exc:  # pragma: no cover
                raise RuntimeError(
                    "borrow_from was requested but wraith.identity is not "
                    f"available: {exc}"
                ) from exc
            cookies = identity_mod.extract_cookies(
                borrow_from,
                domain_filter=host or None,
            )
            identity_mod.inject_cookies(context, cookies)

        page.goto(landing, wait_until="domcontentloaded")
        harvester.wait(timeout=timeout)
        harvester.detach()

    if harvester.captured is None:
        raise RuntimeError(
            "timed out without capturing an authenticated request to "
            f"{target_url!r}. Make sure you logged in / the page issued an "
            "Authorization-bearing API call before the timeout."
        )
    return harvester.save_session(out_path)


def _resolve_context(session: Any) -> Any:
    """Best-effort extraction of a BrowserContext from an engine session."""
    for attr in ("context", "browser_context"):
        ctx = getattr(session, attr, None)
        if ctx is not None:
            return ctx
    # A raw BrowserContext already has .on / .new_page.
    if hasattr(session, "on") and hasattr(session, "new_page"):
        return session
    # A Browser exposes .contexts; reuse the first or make one.
    contexts: Optional[Iterable[Any]] = getattr(session, "contexts", None)
    if contexts:
        contexts = list(contexts)
        if contexts:
            return contexts[0]
    if hasattr(session, "new_context"):
        return session.new_context()
    raise RuntimeError(
        "could not resolve a BrowserContext from the engine session "
        f"(type={type(session).__name__})"
    )


def _resolve_page(session: Any, context: Any) -> Any:
    """Best-effort extraction of a Page; create one if needed."""
    page = getattr(session, "page", None)
    if page is not None:
        return page
    pages = getattr(context, "pages", None)
    if pages:
        pages = list(pages)
        if pages:
            return pages[0]
    return context.new_page()
