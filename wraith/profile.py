"""Profile sync — gather a login on the laptop, use it on a remote Wraith.

This is the orchestration that the Browser-Use "sync your local cookies to
cloud" demo showed, done Wraith's way: end-to-end encrypted, no account, no
inbound port. It ties three existing pieces together:

    source (chrome / firefox / zen / login)   ->   a domain-scoped cookie jar
        wraith.chrome + wraith.identity                (Playwright storageState)
                                              ->   sealed dead-drop transport
                                                       wraith.deaddrop

The agent never sees the jar: ``sync`` runs as a laptop CLI process and prints a
one-shot pairing code; the remote side calls :func:`receive_profile` (exposed as
an MCP tool) which pulls the jar and injects it into the live browser context,
exactly like ``borrow`` does for a local profile.

Cookies are real auth — the session past 2FA — so the flow is domain-scoped by
default. A full-profile sync needs an explicit opt-in from the caller.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from . import identity
from .identity import Cookie

__all__ = [
    "capture_login_jar",
    "gather_cookies",
    "jar_from_cookies",
    "jar_summary",
    "receive_profile",
    "sync_profile",
]

_DISK_SOURCES = ("chrome", "firefox", "zen", "auto")


# --------------------------------------------------------------------------- #
# Gather cookies from an on-disk profile
# --------------------------------------------------------------------------- #

def gather_cookies(
    source: str,
    domain: str | None,
    *,
    profile: str | None = None,
    browser: str | None = None,
) -> list[Cookie]:
    """Read cookies for ``domain`` from an on-disk browser profile.

    ``source`` is ``chrome``/``firefox``/``zen``/``auto``. ``auto`` tries Zen,
    then Firefox, then Chrome. ``profile`` overrides auto-detection with an
    explicit profile-directory path. ``domain`` scopes the export to that domain
    and its subdomains; pass ``None`` only for a deliberate full-profile sync.
    """
    src = (source or "auto").strip().lower()
    if src not in _DISK_SOURCES:
        raise ValueError(f"unknown disk source {source!r}; use one of {_DISK_SOURCES}")

    order = (
        [src]
        if src != "auto"
        else ["zen", "firefox", "chrome"]
    )
    errors: list[str] = []
    for candidate in order:
        try:
            path = profile or _find_profile(candidate)
        except FileNotFoundError as exc:
            errors.append(str(exc))
            continue
        if candidate == "chrome":
            from . import chrome  # lazy: pulls in cryptography

            return chrome.extract_chrome_cookies(path, domain, browser=browser)
        return identity.extract_cookies(path, domain_filter=domain)
    raise FileNotFoundError(
        "no usable browser profile found for source "
        f"{source!r}: {'; '.join(errors) or 'none detected'}"
    )


def _find_profile(kind: str) -> Any:
    if kind == "zen":
        found = identity.find_zen_profiles()
    elif kind == "firefox":
        found = identity.find_firefox_profiles()
    elif kind == "chrome":
        p = identity.find_chrome_profile()
        found = [p] if p else []
    else:
        found = []
    if not found:
        raise FileNotFoundError(f"no {kind} profile detected")
    return found[0]


# --------------------------------------------------------------------------- #
# Jar (Playwright storageState) build + summary
# --------------------------------------------------------------------------- #

def jar_from_cookies(cookies: list[Cookie]) -> dict[str, Any]:
    """Build a Playwright storageState jar from :class:`Cookie` objects."""
    return {"cookies": identity.to_playwright_cookies(cookies), "origins": []}


def jar_summary(jar: dict[str, Any]) -> dict[str, int]:
    """Return a ``{domain: cookie_count}`` map for the confirm step.

    Domains only — never cookie values — so a summary is safe to print.
    """
    counts: Counter[str] = Counter()
    for c in jar.get("cookies", []):
        counts[c.get("domain", "")] += 1
    return dict(counts.most_common())


def jar_to_bytes(jar: dict[str, Any]) -> bytes:
    return json.dumps(jar, separators=(",", ":")).encode("utf-8")


def jar_from_bytes(raw: bytes) -> dict[str, Any]:
    return json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------------- #
# --from login: capture a jar by signing in manually
# --------------------------------------------------------------------------- #

def capture_login_jar(
    url: str,
    domain: str | None,
    *,
    engine: str = "camoufox",
    wait: Any = None,
    headless: bool = False,
) -> dict[str, Any]:
    """Open a real browser window, let a human sign in, capture the jar.

    This is the universal source: it needs no OS keychain and works on any OS.
    Wraith opens ``url`` headed; the human logs in (password manager and 2FA
    included), then ``wait`` is called (default: block on stdin) before the
    session's storageState is captured and scoped to ``domain``.
    """
    from . import engine as engine_mod  # lazy: needs the browser stack

    if wait is None:
        def wait() -> None:  # pragma: no cover - interactive
            input(
                "\nSign in to the site in the opened window, then press Enter "
                "here to capture the session... "
            )

    with engine_mod.launch(engine, headless=headless) as session:
        context = getattr(session, "context", None) or session
        page = getattr(session, "page", None) or context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        wait()
        state = context.storage_state()

    cookies = state.get("cookies", [])
    if domain:
        cookies = [
            c for c in cookies if identity._domain_matches(c.get("domain", ""), domain)
        ]
    origins = state.get("origins", []) if not domain else []
    return {"cookies": cookies, "origins": origins}


# --------------------------------------------------------------------------- #
# Sync (sender) and receive (remote)
# --------------------------------------------------------------------------- #

def sync_profile(
    relay_url: str,
    *,
    source: str = "auto",
    domain: str | None,
    profile: str | None = None,
    browser: str | None = None,
    login_url: str | None = None,
    secret: bytes | None = None,
) -> tuple[str, dict[str, int]]:
    """Gather a domain-scoped jar and push it to the relay.

    Returns ``(pairing_code, summary)``. Hand the pairing code to the remote out
    of band; it is valid for one pickup within the drop's short TTL.
    """
    from . import deaddrop

    if source == "login":
        if not login_url:
            raise ValueError("source 'login' needs login_url")
        jar = capture_login_jar(login_url, domain)
    else:
        cookies = gather_cookies(source, domain, profile=profile, browser=browser)
        jar = jar_from_cookies(cookies)

    if not jar["cookies"]:
        raise RuntimeError(
            f"no cookies found for domain {domain!r} — are you signed in there "
            f"in the {source} profile?"
        )

    summary = jar_summary(jar)
    code = deaddrop.push(relay_url, jar_to_bytes(jar), secret=secret)
    return code, summary


def receive_profile(code: str, *, max_age: int = 600) -> dict[str, Any]:
    """Pull and open the jar named by a pairing ``code`` (remote side).

    Returns the storageState jar dict. The caller injects ``jar["cookies"]``
    into the live browser context (see the ``receive_profile`` MCP tool).
    """
    from . import deaddrop

    raw = deaddrop.pull(code, max_age=max_age)
    return jar_from_bytes(raw)
