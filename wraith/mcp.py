"""Wraith MCP server — drive the stealth/agent browser over the Model Context
Protocol.

This exposes Wraith's :class:`~wraith.agent.AgentBrowser` (browser-use-style
perception + action over the Camoufox stealth engine) as a set of MCP tools so
an LLM client (Claude Desktop / Claude Code, etc.) can navigate, perceive, and
act on real web pages — passing WAAP challenges and, crucially, *borrowing* a
warmed, already-authenticated identity from a real on-disk browser profile.

Design notes
------------
* A single lazily-created :class:`AgentBrowser` is shared across tool calls, so
  the same tab/session (and any borrowed identity) persists for the whole MCP
  session. ``import wraith.mcp`` on its own never touches Playwright/Camoufox.
* **Threading:** ``AgentBrowser`` uses the *sync* Playwright API (Camoufox),
  which refuses to run inside an asyncio event loop and whose objects are
  thread-affine. FastMCP runs tools in the event loop, so every browser
  operation is dispatched to a single dedicated worker thread via a
  ``max_workers=1`` executor (``_run``). The browser is created and used only on
  that thread, which keeps Playwright happy and the session consistent.
* Heavy imports (``wraith.agent`` etc.) are done lazily inside the worker so
  ``import wraith.mcp`` succeeds even without a browser binary installed.

Run it
------
``python -m wraith.mcp`` / the ``wraith-mcp`` console script / ``wraith mcp``.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .agent import AgentBrowser

T = TypeVar("T")

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
# Single-thread browser worker (sync Playwright must stay off the event loop)
# --------------------------------------------------------------------------- #
_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wraith-browser")
_browser: "Optional[AgentBrowser]" = None


async def _run(fn: "Callable[[], T]") -> T:
    """Dispatch ``fn`` to the dedicated browser worker thread and await it."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXEC, fn)


def _get_browser() -> "AgentBrowser":
    """Return the shared AgentBrowser, launching it on first use.

    MUST be called on the worker thread (i.e. from inside a function passed to
    :func:`_run`), because the AgentBrowser owns thread-affine sync Playwright
    objects.
    """
    global _browser
    if _browser is None:
        from .agent import AgentBrowser  # lazy: needs the browser stack

        _browser = AgentBrowser()
    return _browser


def _reset_browser() -> None:
    global _browser
    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None


def _ctx_from_browser(browser: "AgentBrowser") -> Any:
    """Best-effort retrieval of the live Playwright BrowserContext."""
    ctx = getattr(browser, "context", None)
    if ctx is not None and hasattr(ctx, "add_cookies"):
        return ctx
    session = getattr(browser, "session", None)
    if session is not None:
        ctx = getattr(session, "context", None)
        if ctx is not None and hasattr(ctx, "add_cookies"):
            return ctx
    page = getattr(browser, "page", None)
    if page is None and session is not None:
        page = getattr(session, "page", None)
    if page is not None:
        ctx = getattr(page, "context", None)
        if ctx is not None and hasattr(ctx, "add_cookies"):
            return ctx
    return None


# --------------------------------------------------------------------------- #
# Tools (async wrappers -> sync browser work on the worker thread)
# --------------------------------------------------------------------------- #
@app.tool()
async def navigate(url: str) -> str:
    """Open a URL and return an indexed snapshot of the page's interactive
    elements.

    Automatically passes WAAP/anti-bot challenges and dismisses common
    cookie/consent banners. Each line is ``[index]<tag role=...>text</tag>``;
    use the index with `click` / `type_text`.
    """
    return await _run(lambda: _get_browser().navigate(url).to_text())


@app.tool()
async def snapshot() -> str:
    """Re-perceive the current page: a fresh indexed snapshot of its interactive
    elements (use after the DOM may have changed)."""
    return await _run(lambda: _get_browser().snapshot().to_text())


@app.tool()
async def click(index: int) -> str:
    """Click the element with the given index (from the latest snapshot) and
    return the resulting snapshot."""
    return await _run(lambda: _get_browser().click(index).to_text())


@app.tool()
async def type_text(index: int, text: str, enter: bool = False) -> str:
    """Type ``text`` into the input with the given index (clears it first; if
    ``enter`` is true, presses Enter to submit). Returns the resulting snapshot."""
    return await _run(lambda: _get_browser().type(index, text, enter=enter).to_text())


@app.tool()
async def scroll(direction: str = "down") -> str:
    """Scroll the page (``"down"`` or ``"up"``) and return a fresh snapshot."""
    return await _run(lambda: _get_browser().scroll(direction=direction).to_text())


@app.tool()
async def read() -> str:
    """Return the current page's readable content as markdown (for extraction /
    summarisation, as opposed to acting on elements)."""
    return await _run(lambda: _get_browser().read())


@app.tool()
async def screenshot() -> str:
    """Capture a screenshot of the current page; save it to a temp PNG and return
    the absolute path."""
    import os
    import tempfile

    fd, path = tempfile.mkstemp(prefix="wraith-shot-", suffix=".png")
    os.close(fd)
    await _run(lambda: _get_browser().screenshot(path=path))
    return path


@app.tool()
def detect_waap(url: str) -> list[str]:
    """Fingerprint a URL's WAAP / anti-bot defenses (Akamai, Cloudflare,
    Reblaze/Link11, DataDome, Incapsula, SiteMinder, reCAPTCHA, ...). Returns a
    list of detected vendor names — empty if none. Passive; no browser needed."""
    from .detect import identify_waap  # lazy: pulls httpx

    return identify_waap(url)


@app.tool()
async def borrow(domain: str, profile: Optional[str] = None) -> str:
    """Borrow a warmed, already-authenticated identity for ``domain`` from a real
    Firefox/Zen profile on this machine and inject its cookies into the live
    browser, so subsequent `navigate` calls load as that logged-in user — the
    core Wraith move for sidestepping reputation-based defenses.

    ``profile`` optionally selects a profile by a path substring; otherwise the
    first Zen profile is used, falling back to the first Firefox profile.
    """
    from .identity import find_firefox_profiles, find_zen_profiles  # lazy

    profiles = list(find_zen_profiles()) + list(find_firefox_profiles())
    if not profiles:
        return "No Firefox/Zen browser profiles found on this machine to borrow from."

    if profile:
        needle = profile.lower()
        matched = [p for p in profiles if needle in str(p).lower()]
        if not matched:
            return (
                f"No profile matching {profile!r}. Available: "
                + ", ".join(str(p) for p in profiles)
            )
        chosen = matched[0]
    else:
        chosen = profiles[0]

    from .identity import extract_cookies  # lazy

    try:
        cookies = extract_cookies(chosen, domain_filter=domain)
    except Exception as exc:
        return f"Failed to extract cookies from {chosen}: {type(exc).__name__}: {exc}"
    if not cookies:
        return (
            f"No cookies for {domain!r} found in profile {chosen}. "
            "Have you logged into that site in that browser?"
        )

    def _inject() -> str:
        browser = _get_browser()
        ctx = _ctx_from_browser(browser)
        if ctx is None:
            return (
                f"Extracted {len(cookies)} cookie(s) for {domain!r} from {chosen}, but "
                "no live context to inject into — call `navigate` first."
            )
        from .identity import inject_cookies  # lazy

        n = inject_cookies(ctx, cookies)
        return (
            f"Borrowed {n} cookie(s) for {domain!r} from {chosen}. The browser now "
            "navigates as that identity — open the site with `navigate`."
        )

    return await _run(_inject)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    """Run the Wraith MCP server (stdio transport by default)."""
    app.run()


if __name__ == "__main__":  # pragma: no cover
    main()
