"""Proxy rotation for Wraith — a tiny, dependency-free proxy pool.

WHY THIS EXISTS:

    The hardest WAAP failure modes Wraith hits are *reputation-of-IP* problems,
    not fingerprint problems. Reblaze/Link11 serves an HTTP 474/481 rate-limit
    tier (``WaapRateLimitedError``) and Akamai/DataDome/PerimeterX silently tank
    a session's behavioral score once an exit IP has been hammered — neither is
    fixable by switching engines or clearing cookies. The only mitigation is a
    *rotating residential proxy* (see the ``WaapRateLimitedError`` docstring in
    ``wraith.engine``).

    ``ProxyPool`` is the rotation primitive that ``engine.clear_challenge``
    consumes via its ``proxy_pool=`` parameter: when an owned session is
    rate-limited or hard-blocked, the engine relaunches against
    ``proxy_pool.next()`` and marks the offending proxy bad.

DESIGN:

    Deliberately dependency-free and network-free — no Playwright, no httpx, no
    sockets. It is pure bookkeeping over a list of proxy URL strings so it is
    fully unit-testable offline. Each proxy string is in the form Playwright /
    httpx accept and that ``engine.launch(proxy=...)`` forwards, e.g.
    ``"http://user:pass@host:port"``. Bare ``"host:port"`` is accepted too and
    normalised to an ``http://`` URL.
"""

from __future__ import annotations

import random
from typing import Iterable

__all__ = ["ProxyPool", "normalize_proxy"]


# Schemes a proxy URL may legitimately carry. Anything not matching is treated
# as a bare ``host:port`` (or ``user:pass@host:port``) and gets an ``http://``.
_KNOWN_SCHEMES = ("http://", "https://", "socks5://", "socks5h://", "socks4://")


def normalize_proxy(s: str) -> str:
    """Normalise a single proxy spec to a full ``scheme://...`` URL string.

    Accepts either form:

    * a bare authority — ``"host:port"`` or ``"user:pass@host:port"`` — which is
      given a default ``http://`` scheme; or
    * an already-schemed URL — ``"http://user:pass@host:port"``,
      ``"socks5://host:port"``, etc. — which is passed through (with surrounding
      whitespace stripped).

    The output is exactly what ``engine.launch(proxy=...)`` forwards to the
    underlying Playwright/httpx engine.

    Raises ``ValueError`` on an empty / whitespace-only spec.
    """
    if not isinstance(s, str):  # defensive: callers sometimes pass None/ints
        raise TypeError(f"proxy must be a str, got {type(s).__name__}")
    proxy = s.strip()
    if not proxy:
        raise ValueError("empty proxy string")

    lowered = proxy.lower()
    if any(lowered.startswith(scheme) for scheme in _KNOWN_SCHEMES):
        return proxy
    # A scheme we don't special-case but that looks like one (``foo://...``):
    # respect the caller's explicit choice rather than double-prefixing.
    if "://" in proxy:
        return proxy
    return f"http://{proxy}"


class ProxyPool:
    """A rotating pool of proxy URL strings.

    Pure bookkeeping: no network, no Playwright. Hand the rotated value to
    ``engine.launch(proxy=...)`` / ``engine.clear_challenge(proxy_pool=...)``.

    Args:
        proxies: an iterable of proxy specs. Each is run through
            :func:`normalize_proxy`, so bare ``"host:port"`` entries are
            accepted alongside full ``"scheme://user:pass@host:port"`` URLs.
            Order is preserved and duplicates are collapsed (first wins).
        strategy: ``"round_robin"`` (default) cycles through the proxies in
            order; ``"random"`` returns a uniformly random *live* proxy.

    Bad proxies (marked via :meth:`mark_bad`) are skipped by both strategies and
    excluded from :meth:`remaining`, ``len()`` and truthiness, but are remembered
    so they are never handed out again this run.
    """

    def __init__(self, proxies: Iterable[str], *, strategy: str = "round_robin") -> None:
        if strategy not in ("round_robin", "random"):
            raise ValueError(
                f"unknown strategy {strategy!r}; expected 'round_robin' or 'random'"
            )
        self.strategy = strategy

        # Normalise + de-dupe while preserving first-seen order.
        normalised: list[str] = []
        seen: set[str] = set()
        for raw in proxies:
            p = normalize_proxy(raw)
            if p not in seen:
                seen.add(p)
                normalised.append(p)
        self._proxies: list[str] = normalised
        self._bad: set[str] = set()
        # Index of the proxy returned by the most recent current()/next().
        # -1 means "nothing handed out yet" so the first next() returns index 0.
        self._idx: int = -1

    # ------------------------------------------------------------------ #
    # Rotation
    # ------------------------------------------------------------------ #
    def current(self) -> str | None:
        """Return the most recently handed-out *live* proxy without advancing.

        Before the first :meth:`next` call (or if every handed-out proxy has
        since been marked bad), this returns the first live proxy, or ``None``
        when the pool is exhausted.
        """
        if not self._proxies:
            return None
        # If we have a valid pointer to a still-live proxy, reuse it.
        if 0 <= self._idx < len(self._proxies):
            candidate = self._proxies[self._idx]
            if candidate not in self._bad:
                return candidate
        # Otherwise fall back to the first live proxy (no advance / no mutation
        # of _idx — current() is non-destructive).
        for p in self._proxies:
            if p not in self._bad:
                return p
        return None

    def next(self) -> str | None:
        """Advance to and return the next live proxy, or ``None`` if exhausted.

        ``round_robin`` walks the list in order, wrapping around and skipping
        any proxy marked bad. ``random`` picks a uniformly random live proxy
        (and updates the pointer so a following :meth:`current` agrees).
        """
        if not self.remaining():
            return None

        if self.strategy == "random":
            live = [
                i for i, p in enumerate(self._proxies) if p not in self._bad
            ]
            self._idx = random.choice(live)
            return self._proxies[self._idx]

        # round_robin: scan forward from just past the current pointer for the
        # next live proxy, wrapping once around the whole list.
        n = len(self._proxies)
        for step in range(1, n + 1):
            i = (self._idx + step) % n
            if self._proxies[i] not in self._bad:
                self._idx = i
                return self._proxies[i]
        return None  # pragma: no cover - remaining() guards this

    # ------------------------------------------------------------------ #
    # Health bookkeeping
    # ------------------------------------------------------------------ #
    def mark_bad(self, proxy: str) -> None:
        """Mark ``proxy`` as bad so it is never handed out again this run.

        Accepts either the normalised value this pool returned or the raw spec
        the caller originally passed (it is normalised before matching), so
        ``mark_bad(pool.next())`` and ``mark_bad("host:port")`` both work. A
        proxy not in this pool is ignored.
        """
        try:
            normalised = normalize_proxy(proxy)
        except (TypeError, ValueError):
            return
        if normalised in self._proxies:
            self._bad.add(normalised)

    def remaining(self) -> int:
        """Number of live (not-marked-bad) proxies left in the pool."""
        return len(self._proxies) - len(self._bad)

    # ------------------------------------------------------------------ #
    # Dunders
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        """Number of live proxies — equivalent to :meth:`remaining`."""
        return self.remaining()

    def __bool__(self) -> bool:
        """True while at least one live proxy remains."""
        return self.remaining() > 0

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"ProxyPool(strategy={self.strategy!r}, "
            f"live={self.remaining()}, total={len(self._proxies)})"
        )
