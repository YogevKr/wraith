"""Wraith MCP server — drive the stealth/agent browser over the Model Context
Protocol.

This exposes Wraith's :class:`~wraith.agent.AgentBrowser` (browser-use-style
perception + action over the Camoufox stealth engine) as a set of MCP tools so
an LLM client (Claude Desktop, etc.) can navigate, perceive, and act on real
web pages — passing WAAP challenges and, crucially, *borrowing* a warmed,
already-authenticated identity from a real on-disk browser profile.

Design notes
------------
* A single lazily-created :class:`AgentBrowser` is shared across tool calls
  (``_get_browser()``), so the same tab/session persists for the whole MCP
  session. The first tool that needs a browser launches it; ``import wraith.mcp``
  on its own never touches Playwright/Camoufox.
* Every heavy import (``wraith.agent``, ``wraith.detect``, ``wraith.identity``)
  is done **lazily inside the tools**. This means ``import wraith.mcp`` succeeds
  even when no browser binary is installed — only *calling* a tool that needs a
  browser will surface a missing-dependency error, as a returned string rather
  than an import-time crash.
* Tool return values are all plain strings (or JSON-serialisable), since MCP
  tools return text content. Snapshots are returned in browser-use ``.to_text()``
  form: one line per interactive element, ``[12]<button role=button>Search</button>``.

Run it
------
``python -m wraith.mcp`` or the installed console script, or point an MCP client
at ``wraith.mcp:app`` / call :func:`main`. Requires the ``mcp`` package
(``pip install mcp``) and a browser engine for the action tools.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime here
    from .agent import AgentBrowser


# --------------------------------------------------------------------------- #
# Server instance
# --------------------------------------------------------------------------- #
app = FastMCP(
    "wraith",
    instructions=(
        "Wraith is a stealth + identity-borrowing browser for autonomous agents. "
        "Use `navigate(url)` to open a page (it auto-passes WAAP challenges and "
        "dismisses cookie banners) and get back an indexed snapshot of interactive "
        "elements. Each line looks like `[12]<button role=button>Search</button>`; "
        "act on an element by its index with `click(index)` or "
        "`type_text(index, text)`. Use `snapshot()` to re-perceive after a change, "
        "`scroll()` to reveal more, `read()` for the page as markdown, and "
        "`screenshot()` to capture an image. `detect_waap(url)` fingerprints a "
        "site's bot defenses. `borrow(domain)` injects a warmed, authenticated "
        "identity (cookies) from a real Firefox/Zen profile on this machine so the "
        "browser navigates as that already-logged-in user."
    ),
)


# --------------------------------------------------------------------------- #
# Lazy singleton browser
# --------------------------------------------------------------------------- #
# Held at module scope so the same AgentBrowser (and therefore the same tab and
# borrowed identity) is reused across tool invocations within an MCP session.
_browser: "Optional[AgentBrowser]" = None


def _get_browser() -> "AgentBrowser":
    """Return the shared AgentBrowser, launching it on first use.

    ``wraith.agent`` is imported here (not at module top) so that merely
    importing ``wraith.mcp`` does not require Playwright/Camoufox to be present.
    """
    global _browser
    if _browser is None:
        from .agent import AgentBrowser  # lazy: needs the browser stack

        _browser = AgentBrowser()
    return _browser


def _reset_browser() -> None:
    """Close and drop the shared browser (best-effort)."""
    global _browser
    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None


def _ctx_from_browser(browser: "AgentBrowser") -> Any:
    """Best-effort retrieval of the live Playwright BrowserContext.

    The AgentBrowser wraps a :class:`wraith.engine.Session` (which exposes
    ``.context`` / ``.page``). We probe a few attribute paths defensively so we
    stay decoupled from the exact attribute name the agent module settles on.
    """
    # Direct passthrough, if exposed.
    ctx = getattr(browser, "context", None)
    if ctx is not None and hasattr(ctx, "add_cookies"):
        return ctx
    # Via the underlying session.
    session = getattr(browser, "session", None)
    if session is not None:
        ctx = getattr(session, "context", None)
        if ctx is not None and hasattr(ctx, "add_cookies"):
            return ctx
    # Via the page's context.
    page = getattr(browser, "page", None)
    if page is None and session is not None:
        page = getattr(session, "page", None)
    if page is not None:
        ctx = getattr(page, "context", None)
        if ctx is not None and hasattr(ctx, "add_cookies"):
            return ctx
    return None


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@app.tool()
def navigate(url: str) -> str:
    """Open a URL and return an indexed snapshot of the page's interactive
    elements.

    Automatically passes WAAP/anti-bot challenges and dismisses common
    cookie/consent banners. The returned text lists one interactive element per
    line as ``[index]<tag role=...>text</tag>``; use the index with `click` /
    `type_text`.
    """
    browser = _get_browser()
    snapshot = browser.navigate(url)
    return snapshot.to_text()


@app.tool()
def snapshot() -> str:
    """Re-perceive the current page: return a fresh indexed snapshot of its
    interactive elements (use after the DOM may have changed)."""
    browser = _get_browser()
    snap = browser.snapshot()
    return snap.to_text()


@app.tool()
def click(index: int) -> str:
    """Click the interactive element with the given index (from the latest
    snapshot) and return the resulting snapshot."""
    browser = _get_browser()
    snap = browser.click(index)
    return snap.to_text()


@app.tool()
def type_text(index: int, text: str, enter: bool = False) -> str:
    """Type ``text`` into the input element with the given index.

    Clears the field first. If ``enter`` is true, presses Enter afterward (e.g.
    to submit a search). Returns the resulting snapshot.
    """
    browser = _get_browser()
    snap = browser.type(index, text, enter=enter)
    return snap.to_text()


@app.tool()
def scroll(direction: str = "down") -> str:
    """Scroll the page (``"down"`` or ``"up"``) to reveal more content, then
    return a fresh snapshot."""
    browser = _get_browser()
    snap = browser.scroll(direction=direction)
    return snap.to_text()


@app.tool()
def read() -> str:
    """Return the current page's readable content as markdown/plain text (good
    for extraction and summarisation, as opposed to acting on elements)."""
    browser = _get_browser()
    return browser.read()


@app.tool()
def screenshot() -> str:
    """Capture a screenshot of the current page. Saves it to a temporary PNG
    file and returns the absolute path."""
    import tempfile

    browser = _get_browser()
    fd, path = tempfile.mkstemp(prefix="wraith-shot-", suffix=".png")
    import os

    os.close(fd)
    browser.screenshot(path=path)
    return path


@app.tool()
def detect_waap(url: str) -> list[str]:
    """Fingerprint a URL's web-application-firewall / anti-bot defenses (e.g.
    Akamai, Cloudflare, PerimeterX/HUMAN, DataDome). Returns a list of detected
    vendor/technology names — empty if none were identified.

    This is a passive diagnostic and does not require the browser.
    """
    from .detect import identify_waap  # lazy: pulls httpx

    return identify_waap(url)


@app.tool()
def borrow(domain: str, profile: Optional[str] = None) -> str:
    """Borrow a warmed, already-authenticated identity for ``domain`` from a
    real browser profile on this machine.

    Extracts cookies scoped to ``domain`` (and its subdomains) from a local
    Firefox/Zen profile and injects them into the live browser context, so
    subsequent `navigate` calls load as that already-logged-in user — the core
    Wraith move for sidestepping reputation-based defenses.

    ``profile`` optionally selects a profile by a substring of its path (e.g.
    ``"default"`` or a profile name); if omitted, the first discovered Zen
    profile is used, falling back to the first Firefox profile.

    Returns a human-readable summary of what was borrowed.
    """
    from .identity import (  # lazy
        extract_cookies,
        find_firefox_profiles,
        find_zen_profiles,
    )

    # Discover candidate profiles (Zen first — it's the preferred warmed source).
    profiles = list(find_zen_profiles()) + list(find_firefox_profiles())
    if not profiles:
        return "No Firefox/Zen browser profiles found on this machine to borrow from."

    if profile:
        needle = profile.lower()
        matched = [p for p in profiles if needle in str(p).lower()]
        if not matched:
            available = ", ".join(str(p) for p in profiles)
            return (
                f"No profile matching {profile!r} found. "
                f"Available profiles: {available}"
            )
        chosen = matched[0]
    else:
        chosen = profiles[0]

    try:
        cookies = extract_cookies(chosen, domain_filter=domain)
    except Exception as exc:  # ChromeEncryptionError, FileNotFoundError, ...
        return f"Failed to extract cookies from {chosen}: {type(exc).__name__}: {exc}"

    if not cookies:
        return (
            f"No cookies for {domain!r} found in profile {chosen}. "
            "Make sure you have visited/logged into that site in that browser."
        )

    # Need a live context to inject into; this launches the browser if needed.
    browser = _get_browser()
    ctx = _ctx_from_browser(browser)
    if ctx is None:
        return (
            f"Extracted {len(cookies)} cookie(s) for {domain!r} from {chosen}, but "
            "could not access the live browser context to inject them. Try calling "
            "`navigate` first to ensure a page is open."
        )

    from .identity import inject_cookies  # lazy

    injected = inject_cookies(ctx, cookies)
    return (
        f"Borrowed {injected} cookie(s) for {domain!r} from profile {chosen}. "
        f"The browser now navigates as that identity — open the site with `navigate`."
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    """Run the Wraith MCP server (stdio transport by default)."""
    app.run()


if __name__ == "__main__":  # pragma: no cover
    main()
