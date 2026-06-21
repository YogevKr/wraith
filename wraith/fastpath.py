"""No-browser TLS-impersonation fast path.

Wraith's expensive work — clearing WAAP JS challenges and producing a warmed,
authenticated session — happens **once** in the real browser (Camoufox /
patchright-Chromium). This module *replays* that work cheaply: a ``curl_cffi``
client that presents a real-browser TLS + HTTP/2 fingerprint and reuses the
cookies / headers that identity-borrowing (:mod:`wraith.identity`) or harvesting
(:class:`wraith.harvest.CapturedSession`) already produced — so an authenticated
API call no longer pays full browser launch cost.

This is the half no comparable project combines: a TLS-impersonation client
*driven by a session borrowed from your own real browser profile*. Capture once
in the browser, then replay many times here.

The TLS preset is chosen to **cohere with the session that minted it**: a
Firefox/Camoufox UA maps to a Firefox impersonation preset, Chrome to Chrome,
etc. (a Chrome-TLS request carrying a Firefox UA is itself a tell).

``curl_cffi`` is an optional dependency:  ``pip install 'wraith[fastpath]'``.
"""
from __future__ import annotations

from typing import Any, Optional

from . import detect, identity

__all__ = [
    "FastPathUnavailableError",
    "ua_to_impersonate",
    "fetch",
    "replay",
    "cookies_from_context",
    "from_context",
    "classify",
]

# Latest presets available in curl_cffi 0.15 (see BrowserType). Firefox is the
# default because Wraith is Camoufox(Firefox)-first.
_FIREFOX = "firefox147"
_CHROME = "chrome146"
_SAFARI = "safari18_0"
_DEFAULT_IMPERSONATE = _FIREFOX


class FastPathUnavailableError(RuntimeError):
    """Raised when the fast path is used without ``curl_cffi`` installed."""


def _require_curl() -> Any:
    try:
        from curl_cffi import requests as _cc  # type: ignore
    except ImportError as exc:  # pragma: no cover - import guard
        raise FastPathUnavailableError(
            "The fast path needs curl_cffi. Install it with: pip install 'wraith[fastpath]'"
        ) from exc
    return _cc


def ua_to_impersonate(user_agent: Optional[str]) -> str:
    """Pick a curl_cffi impersonation preset coherent with a User-Agent string.

    Firefox/Camoufox -> Firefox preset (the default), Chrome/Chromium/Edge ->
    Chrome, Safari (non-Chromium) -> Safari. An unknown/empty UA defaults to
    Firefox so the TLS fingerprint matches Wraith's Camoufox-first identity.
    """
    ua = (user_agent or "").lower()
    if "firefox" in ua or "gecko/" in ua and "like gecko" not in ua:
        return _FIREFOX
    if "edg/" in ua or "chrome" in ua or "chromium" in ua:
        return _CHROME
    if "safari" in ua and "chrome" not in ua:
        return _SAFARI
    return _DEFAULT_IMPERSONATE


def _coerce_cookies(cookies: Any) -> Any:
    """Normalize cookies into something curl_cffi accepts (dict or CookieJar)."""
    if cookies is None:
        return None
    if isinstance(cookies, dict):
        return cookies
    items = list(cookies)
    if not items:
        return None
    # CookieJar is iterable of http.cookiejar.Cookie; pass it through untouched.
    import http.cookiejar as _cj
    if isinstance(cookies, _cj.CookieJar):
        return cookies
    # list[identity.Cookie] or list[playwright dict] -> jar
    return identity.to_cookiejar(items)


def _proxies(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def fetch(
    url: str,
    *,
    method: str = "GET",
    session: Any = None,
    cookies: Any = None,
    headers: Optional[dict] = None,
    impersonate: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = 30.0,
    allow_redirects: bool = True,
    **kwargs: Any,
) -> Any:
    """Issue one no-browser request with a real-browser TLS fingerprint.

    :param session: an optional :class:`wraith.harvest.CapturedSession` (or any
        object exposing ``authorization`` / ``cookie`` / ``user_agent``, or a
        ``to_dict()`` with a ``headers`` map). Its ``Authorization`` / ``Cookie``
        / ``User-Agent`` are applied and the impersonation preset is derived from
        its UA unless ``impersonate`` is given.
    :param cookies: borrowed cookies — ``identity.Cookie`` objects, Playwright
        cookie dicts, a ``CookieJar``, or a ``{name: value}`` dict.
    :param impersonate: explicit curl_cffi preset; otherwise inferred from the
        session/headers UA (default Firefox).
    :returns: the ``curl_cffi`` Response (``.status_code`` / ``.text`` /
        ``.headers`` / ``.json()`` / ``.cookies``). Pass it to
        :func:`classify` to decide whether to escalate to the browser.
    """
    cc = _require_curl()
    hdrs: dict[str, str] = {}
    ua: Optional[str] = None

    if session is not None:
        sess_headers = None
        if hasattr(session, "to_dict"):
            try:
                sess_headers = (session.to_dict() or {}).get("headers")
            except Exception:
                sess_headers = None
        if sess_headers:
            hdrs.update({k: v for k, v in sess_headers.items() if v})
        else:
            if getattr(session, "authorization", None):
                hdrs["Authorization"] = session.authorization
            if getattr(session, "cookie", None):
                hdrs["Cookie"] = session.cookie
            if getattr(session, "user_agent", None):
                hdrs["User-Agent"] = session.user_agent
        ua = hdrs.get("User-Agent") or getattr(session, "user_agent", None)

    if headers:
        hdrs.update(headers)
    ua = ua or hdrs.get("User-Agent")

    imp = impersonate or ua_to_impersonate(ua)
    jar = _coerce_cookies(cookies)

    return cc.request(
        method.upper(),
        url,
        headers=hdrs or None,
        cookies=jar,
        impersonate=imp,
        proxies=_proxies(proxy),
        timeout=timeout,
        allow_redirects=allow_redirects,
        **kwargs,
    )


def replay(session: Any, url: str, **kwargs: Any) -> Any:
    """Replay a captured/borrowed session against ``url`` with no browser.

    Thin wrapper over :func:`fetch` for the common case of feeding a
    :class:`wraith.harvest.CapturedSession` straight into a cheap request.
    """
    return fetch(url, session=session, **kwargs)


def cookies_from_context(context: Any) -> Any:
    """Build a CookieJar from a *live* Playwright browser context.

    The browser↔fast-path handoff: after the browser has done the expensive
    challenge-clearing / login, lift its cookies into a jar the fast path can
    replay with.
    """
    try:
        pw_cookies = context.cookies()
    except Exception:
        pw_cookies = []
    return identity.to_cookiejar(pw_cookies)


def from_context(
    context: Any,
    url: str,
    *,
    user_agent: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Hand off a live browser context to the fast path and fetch ``url``.

    Pulls the context's cookies (and, when available, its real User-Agent from
    an open page) and issues a cheap request that coheres with the browser that
    cleared the challenge. This is the canonical "do it once in Camoufox, then
    replay cheaply" path.
    """
    jar = cookies_from_context(context)
    if user_agent is None:
        try:
            pages = getattr(context, "pages", None) or []
            if pages:
                user_agent = pages[0].evaluate("() => navigator.userAgent")
        except Exception:
            user_agent = None
    headers = {"User-Agent": user_agent} if user_agent else None
    return fetch(url, cookies=jar, headers=headers, **kwargs)


def classify(response: Any) -> "detect.ResponseSignal":
    """Classify a fast-path Response via :func:`wraith.detect.classify_response`."""
    body = ""
    try:
        body = response.text or ""
    except Exception:
        body = ""
    return detect.classify_response(response.status_code, dict(response.headers), body)
