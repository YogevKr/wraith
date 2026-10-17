"""Domain allowlist: keep an agent browser on the sites you trust.

A prompt injection in page text can steer an agent to another site and leak
data in the URL. With a :class:`DomainPolicy`, the browser context refuses
requests and WebSockets to every other host, :class:`~wraith.agent.AgentBrowser`
refuses navigations before they start, and pages that a server redirect moves
off the list are closed before the agent reads them.

Patterns are host names. ``example.com`` matches only that host;
``*.example.com`` matches the host and every subdomain. Ports and schemes are
not part of the match. ``about:``, ``data:``, ``blob:`` and ``javascript:``
URLs have no network host and stay allowed; other schemes (``file:``) are
refused.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

__all__ = ["DomainNotAllowedError", "DomainPolicy"]

_NETWORK_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_LOCAL_SCHEMES = frozenset({"about", "data", "blob", "javascript"})
_HOST_PORT = re.compile(r"[^:]+:\d+")


class DomainNotAllowedError(PermissionError):
    """A URL's host is outside the browser's domain allowlist."""


class DomainPolicy:
    """Hosts an agent browser may contact, and the routes that enforce them."""

    def __init__(self, patterns: Iterable[str]) -> None:
        cleaned = []
        for pattern in patterns:
            value = str(pattern).strip().lower().rstrip(".")
            if not value:
                continue
            if "/" in value or _HOST_PORT.fullmatch(value) or value in {"*", "*."}:
                raise ValueError(f"{pattern!r} is not a host name pattern (no scheme or port)")
            cleaned.append(value)
        if not cleaned:
            raise ValueError("the domain allowlist is empty")
        self.patterns: tuple[str, ...] = tuple(cleaned)
        #: How many top-level navigations the routes refused, and the last URL.
        self.blocked_navigations = 0
        self.last_blocked: Optional[str] = None

    @classmethod
    def parse(cls, value: Any) -> Optional["DomainPolicy"]:
        """Build a policy from ``"a.com,*.b.com"`` or a list; ``None`` when empty."""
        if value is None or isinstance(value, DomainPolicy):
            return value
        items = value.split(",") if isinstance(value, str) else list(value)
        if not any(str(item).strip() for item in items):
            return None
        return cls(items)

    def allows(self, url: str) -> bool:
        """Whether the browser may load ``url``."""
        try:
            parts = urlsplit(str(url))
        except ValueError:
            return False
        scheme = parts.scheme.lower()
        if scheme in _LOCAL_SCHEMES:
            return True
        if scheme not in _NETWORK_SCHEMES:
            return False
        host = (parts.hostname or "").rstrip(".")
        return bool(host) and any(_matches(host, p) for p in self.patterns)

    def check(self, url: str) -> None:
        """Raise :class:`DomainNotAllowedError` when ``url`` is not allowed."""
        if not self.allows(url):
            raise DomainNotAllowedError(f"{_where(url)} is not in the domain allowlist")

    def raise_if_blocked(self, mark: int) -> None:
        """Raise when the routes refused a top-level navigation after ``mark``.

        ``mark`` is :attr:`blocked_navigations` before the action.
        """
        if self.blocked_navigations > mark:
            raise DomainNotAllowedError(
                f"Blocked a navigation: {_where(self.last_blocked or '')} is not in "
                "the domain allowlist. The page did not change."
            )

    def close_if_outside(self, page: Any) -> None:
        """Close ``page`` when a server redirect moved it off the list.

        Playwright does not route a redirect, so the page URL is the check.
        """
        url = page.url
        if self.allows(url):
            return
        with contextlib.suppress(Exception):
            page.goto("about:blank")
        raise DomainNotAllowedError(
            f"{_where(url)} is not in the domain allowlist. A redirect moved the "
            "page there, so Wraith closed it."
        )

    def install(self, context: Any) -> None:
        """Refuse requests and WebSockets to other hosts in ``context``.

        Playwright routes every request through the predicate; routing turns
        off the browser's HTTP cache. A server redirect is not routed, so
        :class:`~wraith.agent.AgentBrowser` also checks the page URL.
        """
        context.route(self._denied, self._abort)
        route_web_socket = getattr(context, "route_web_socket", None)
        if callable(route_web_socket):
            route_web_socket(self._denied, self._close)

    def _denied(self, url: str) -> bool:
        return not self.allows(url)

    def _abort(self, route: Any) -> None:
        request = route.request
        try:
            if request.is_navigation_request() and request.frame.parent_frame is None:
                self.blocked_navigations += 1
                self.last_blocked = request.url
        except Exception:
            pass
        route.abort("blockedbyclient")

    @staticmethod
    def _close(ws: Any) -> None:
        ws.close(code=1008, reason="Host is not in the domain allowlist")


def _matches(host: str, pattern: str) -> bool:
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    return host == pattern


def _where(url: str) -> str:
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return repr(url)
    return parts.hostname or f"{parts.scheme}: URL"
