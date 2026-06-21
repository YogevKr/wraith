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

try:  # Image content block (vision); optional across mcp SDK versions.
    from mcp.server.fastmcp import Image
except Exception:  # pragma: no cover - older/newer SDK without the helper
    Image = None  # type: ignore[assignment]

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
        "browser navigates as that already-logged-in user. "
        "`ensure_high_score(url)` borrows a logged-in Google identity's "
        "reputation cookies and opens the URL so a reCAPTCHA-v3 score is minted "
        "high (general across any sitekey) — opt-in; verify acceptance against "
        "the real protected endpoint."
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


def _get_browser(reputation: Optional[Any] = None) -> "AgentBrowser":
    """Return the shared AgentBrowser, launching it on first use.

    MUST be called on the worker thread (i.e. from inside a function passed to
    :func:`_run`), because the AgentBrowser owns thread-affine sync Playwright
    objects.

    Args:
        reputation: Optional :class:`wraith.recaptcha_v3.ReputationSource`. The
            source must be wired in at *construction* time so the engine launches
            with the un-partition firefox prefs (3rd-party google.com cookies
            must reach the reCAPTCHA iframe). If a browser already exists without
            this source, it is torn down and re-created with it; if it already
            has a reputation source, it is reused as-is.
    """
    global _browser
    if reputation is not None and _browser is not None and getattr(
        _browser, "reputation", None
    ) is None:
        # An existing un-reputationed browser can't be retrofitted with the
        # launch-time un-partition prefs — recycle it so the next construction
        # picks them up. (No live session is lost on first navigate.)
        _reset_browser()
    if _browser is None:
        from .agent import AgentBrowser  # lazy: needs the browser stack

        _browser = AgentBrowser(reputation=reputation)
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
def _render(snap: Any, include_snapshot: bool = True) -> str:
    """Full indexed snapshot, or a compact summary when the caller doesn't need it.

    Action tools default to returning the full snapshot, but pass
    ``include_snapshot=false`` to save tokens — you then get the URL, what
    changed, and the element count (call ``snapshot()`` for the indexed list).
    """
    if include_snapshot:
        return snap.to_text()
    head = f"URL: {snap.url}"
    changed = getattr(snap, "changed", None)
    if changed:
        head += f" | {changed}"
    return head + f" | {len(snap.elements)} interactive elements (call snapshot() for the indexed list)"


@app.tool()
async def navigate(url: str, include_snapshot: bool = True) -> str:
    """Open a URL and return an indexed snapshot of the page's interactive
    elements.

    Automatically passes WAAP/anti-bot challenges and dismisses common
    cookie/consent banners. Each line is ``[index]<tag role=...>text</tag>``;
    use the index with `click` / `type_text`. Pass ``include_snapshot=false`` for
    a compact summary (saves tokens; call `snapshot()` when you need the list).
    """
    return await _run(lambda: _render(_get_browser().navigate(url), include_snapshot))


@app.tool()
async def snapshot() -> str:
    """Re-perceive the current page: a fresh indexed snapshot of its interactive
    elements (use after the DOM may have changed)."""
    return await _run(lambda: _get_browser().snapshot().to_text())


@app.tool()
async def click(index: int, include_snapshot: bool = True) -> str:
    """Click the element with the given index (from the latest snapshot).

    Returns the resulting snapshot (or a compact change summary when
    ``include_snapshot=false``). The result's ``Changed:`` line reports what the
    click did (url change / new elements / nothing)."""
    return await _run(lambda: _render(_get_browser().click(index), include_snapshot))


@app.tool()
async def type_text(index: int, text: str, enter: bool = False, include_snapshot: bool = True) -> str:
    """Type ``text`` into the input with the given index (clears it first; if
    ``enter`` is true, presses Enter to submit). Returns the resulting snapshot
    (or a compact summary when ``include_snapshot=false``)."""
    return await _run(
        lambda: _render(_get_browser().type(index, text, enter=enter), include_snapshot)
    )


@app.tool()
async def scroll(direction: str = "down", include_snapshot: bool = True) -> str:
    """Scroll the page (``"down"`` or ``"up"``) and return a fresh snapshot
    (or a compact summary when ``include_snapshot=false``)."""
    return await _run(lambda: _render(_get_browser().scroll(direction=direction), include_snapshot))


@app.tool()
async def browser_tabs(action: str = "list", index: int = 0, url: str = "") -> str:
    """Manage tabs. ``action``: ``list`` (default), ``select`` (by ``index``),
    ``new`` (optionally open ``url``), or ``close`` (by ``index``). Returns the
    tab list, or the new active tab's snapshot for ``select``/``new``."""
    def _go() -> str:
        br = _get_browser()
        act = action.lower()
        if act == "select":
            return br.select_tab(index).to_text()
        if act == "new":
            return br.new_tab(url or None).to_text()
        if act == "close":
            tabs = br.close_tab(index)
        else:
            tabs = br.tabs()
        return "\n".join(
            f"[{t['index']}]{'*' if t['active'] else ' '} {t['title']}  {t['url']}" for t in tabs
        ) or "(no tabs)"

    return await _run(_go)


@app.tool()
async def save_state(path: str) -> str:
    """Export the current session (cookies + localStorage) to a Playwright
    storageState JSON at ``path`` — a portable, reusable authenticated session."""
    return await _run(lambda: f"saved storageState -> {_get_browser().save_storage_state(path)}")


@app.tool()
async def read() -> str:
    """Return the current page's readable content as markdown (for extraction /
    summarisation, as opposed to acting on elements)."""
    return await _run(lambda: _get_browser().read())


@app.tool()
async def screenshot() -> Any:
    """Capture a screenshot of the current page.

    Returns an inline PNG image (so a multimodal model can see the page and
    disambiguate by the same element indices). On an SDK without image content
    support, falls back to saving a temp PNG and returning its path."""
    png = await _run(lambda: _get_browser().screenshot())
    if Image is not None:
        return Image(data=png, format="png")
    import os
    import tempfile

    fd, path = tempfile.mkstemp(prefix="wraith-shot-", suffix=".png")
    with os.fdopen(fd, "wb") as fh:
        fh.write(png)
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


@app.tool()
async def ensure_high_score(url: str, profile: Optional[str] = None) -> str:
    """Borrow a logged-in Google identity's reputation and open ``url`` so a
    reCAPTCHA-v3 score is minted high.

    This is the GENERAL reCAPTCHA-v3 pass: the v3 score is computed inside the
    google.com reCAPTCHA iframe from the .google.com reputation cookies present
    in the context, so injecting a warmed Google identity's cookies (delivered
    3rd-party with secure+SameSite=None into an un-partitioned context) lifts the
    score across any sitekey/site. The browser is (re)launched with the
    un-partition firefox prefs, the reputation cookies are injected, then the URL
    is navigated (passing any WAAP and dismissing consent first).

    ``profile`` optionally selects the source Firefox/Zen profile by a path
    substring; otherwise the first Zen profile is used, falling back to Firefox.

    WARNING: borrowing your *primary* Google identity carries
    anomalous-session / 2FA risk — this is opt-in. After it returns, VERIFY
    success against the real protected endpoint (accept vs reject); the v3 score
    is run-variable and there is no trustworthy score readout for a 3rd-party
    sitekey. Returns the detected reCAPTCHA params and whether the reload request
    carried the reputation cookies.
    """

    def _go() -> str:
        try:
            from .recaptcha_v3 import BorrowedGoogleCookies
        except Exception as exc:
            return (
                "wraith: the reCAPTCHA-v3 capability is unavailable "
                f"({type(exc).__name__}: {exc})."
            )
        source = BorrowedGoogleCookies(profile_substring=profile)
        browser = _get_browser(reputation=source)
        snap = browser.navigate(url)
        params = browser._ensure_high_score()
        n = snap.to_text().count("\n") + 1 if snap else 0
        if params is None:
            return (
                f"Navigated to {url} with borrowed Google reputation, but no "
                "reCAPTCHA params were resolved (recaptcha_v3 unavailable or no "
                f"reCAPTCHA on page). Snapshot has ~{n} interactive line(s). "
                "Verify acceptance against the real protected endpoint."
            )
        return (
            f"Navigated to {url} with borrowed Google reputation. "
            f"reCAPTCHA: version={getattr(params, 'version', '?')}, "
            f"enterprise={getattr(params, 'enterprise', '?')}, "
            f"host={getattr(params, 'host', '?')}, "
            f"sitekey={getattr(params, 'sitekey', '') or '(none)'}. "
            "The v3 score is run-variable with no trustworthy readout for a "
            "3rd-party sitekey — verify acceptance against the real protected "
            "endpoint."
        )

    return await _run(_go)


@app.tool()
async def fetch(
    url: str,
    session_file: str = "",
    method: str = "GET",
    impersonate: str = "",
) -> str:
    """No-browser TLS-impersonation request — the cheap fast path.

    Replays a captured session against ``url`` with a real-browser TLS+HTTP2
    fingerprint and NO browser launch. ``session_file`` is a JSON file with
    ``{headers:{Authorization,Cookie,User-Agent}}`` (as written by
    ``wraith harvest`` / the borrow flow). Use this to replay an
    already-authenticated/cleared session cheaply; escalate to ``navigate`` only
    when the returned classification is ``challenge``. Returns the status,
    classification, and a body preview.
    """
    import asyncio
    import json as _json

    from . import fastpath

    headers = None
    if session_file:
        with open(session_file) as fh:
            data = _json.load(fh)
        headers = data.get("headers") or data

    def _go() -> str:
        resp = fastpath.fetch(url, method=method, headers=headers, impersonate=impersonate or None)
        sig = fastpath.classify(resp)
        head = f"{resp.status_code}  {sig.state}"
        if sig.vendor:
            head += f" [{sig.vendor}]"
        if sig.reason:
            head += f"  — {sig.reason}"
        body = (getattr(resp, "text", "") or "")[:1500]
        return head + "\n\n" + body

    try:
        return await asyncio.to_thread(_go)
    except fastpath.FastPathUnavailableError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    """Run the Wraith MCP server (stdio transport by default)."""
    app.run()


if __name__ == "__main__":  # pragma: no cover
    main()
