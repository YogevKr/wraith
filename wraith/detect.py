"""Diagnostics — let Wraith self-assess how it looks to the defenses it faces.

Three probes, each tuned to a hard-won, empirically-verified gotcha from
driving EL AL's stack (Reblaze/Link11 ac_v2 + reCAPTCHA-v3 + Akamai +
SiteMinder):

* :func:`recaptcha_v3_score` — read the actual reputation score Google
  assigns the *current* browser identity. v3 has no solver; it is a 0.0..1.0
  reputation number (Google account cookies + aged history + exit IP). A
  fresh automated profile scores ~0.1-0.3 no matter the engine; a warmed
  real browser scores ~0.9. This probe tells you which side of that line the
  current identity is on, so you know whether to fall back to *identity
  borrowing* (inject a real warmed session) instead of fighting the gate.

* :func:`bot_detector` — run rebrowser's bot-detector and scrape the
  individual automation tells (Runtime.enable CDP leak, navigator.webdriver,
  the tell-tale 1280x720 default viewport, Playwright init-script leak, etc.)
  so you can confirm the stealth backend is actually suppressing them.

* :func:`identify_waap` — fingerprint which WAAP/anti-bot vendor sits in
  front of a URL from its response headers, cookies and status codes, so the
  caller can pick the right engine/strategy before wasting a session.

httpx is used for cheap header-only probes; Playwright is used wherever the
signal only exists after JavaScript runs (the reCAPTCHA score and the
bot-detector results are both JS-rendered).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Union

import httpx

__all__ = [
    "RECAPTCHA_V3_TEST_URL",
    "BOT_DETECTOR_URL",
    "recaptcha_v3_score",
    "bot_detector",
    "identify_waap",
]

# Public test endpoints used by the JS-rendered probes.
RECAPTCHA_V3_TEST_URL = "https://cleantalk.org/recaptcha-v3-score-test"
BOT_DETECTOR_URL = "https://bot-detector.rebrowser.net/"

# How fresh the reCAPTCHA result line's timestamp must be (seconds) for us to
# trust it as belonging to *this* run rather than a cached "Last Score".
_RECAPTCHA_FRESHNESS_S = 120.0


# ---------------------------------------------------------------------------
# reCAPTCHA-v3 score
# ---------------------------------------------------------------------------

# The canonical, machine-generated result line on cleantalk's tester looks
# like:
#
#     Result: 0.9 | Time: 2026-06-20 14:03:11 | Hostname: cleantalk.org
#
# We parse ONLY this line. We DELIBERATELY do not regex the page generically,
# because:
#   1. the page's FAQ section literally contains the string "0.9" as example
#      copy, so a naive `0\.\d` search over the whole DOM yields a false 0.9;
#   2. the tester caches and displays a stale "Last Score" from a previous
#      visitor/run; reading that gives a score for an identity that isn't ours.
# Both of these bit us in practice. The fix is twofold: anchor on the literal
# "Result: ... | Time: ... | Hostname:" shape, AND verify the Time field is
# fresh (within _RECAPTCHA_FRESHNESS_S of now) so a cached line is rejected.
_RESULT_LINE_RE = re.compile(
    r"Result:\s*(?P<score>[01](?:\.\d+)?)\s*\|\s*"
    r"Time:\s*(?P<time>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s*\|\s*"
    r"Hostname:\s*(?P<hostname>\S+)",
    re.IGNORECASE,
)

# Selectors the score is most likely rendered into; falls back to full text.
_RECAPTCHA_RESULT_SELECTORS = (
    "#recaptcha_result",
    ".recaptcha-result",
    "#result",
    ".result",
)


def _parse_recaptcha_time(raw: str) -> datetime:
    """Parse the tester's ``Time:`` field into an aware UTC datetime.

    The tester reports server time (cleantalk runs UTC). We normalise both the
    'T' and ' ' separators and attach UTC so freshness math is unambiguous.
    """
    cleaned = raw.strip().replace("T", " ")
    dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)


def _extract_fresh_score(text: str, *, now: float | None = None) -> float:
    """Find the real result line in ``text`` and return its score if fresh.

    Raises ``ValueError`` if no canonical line is present or the most recent
    canonical line is stale (a cached "Last Score").
    """
    now = time.time() if now is None else now
    matches = list(_RESULT_LINE_RE.finditer(text))
    if not matches:
        raise ValueError(
            "no canonical 'Result: X | Time: ... | Hostname:' line found "
            "(refusing to guess from page text — the FAQ contains a decoy "
            "'0.9' and a stale 'Last Score')"
        )

    # If multiple canonical lines exist, prefer the freshest by timestamp.
    best: tuple[float, float] | None = None  # (age_seconds, score)
    last_error: str | None = None
    for m in matches:
        try:
            ts = _parse_recaptcha_time(m.group("time"))
        except ValueError as exc:  # pragma: no cover - defensive
            last_error = str(exc)
            continue
        age = now - ts.timestamp()
        score = float(m.group("score"))
        if best is None or age < best[0]:
            best = (age, score)

    if best is None:
        raise ValueError(f"could not parse Time field on any result line: {last_error}")

    age, score = best
    if age > _RECAPTCHA_FRESHNESS_S:
        raise ValueError(
            f"result line is stale (timestamp is {age:.0f}s old, "
            f"max {_RECAPTCHA_FRESHNESS_S:.0f}s) — this is a cached "
            "'Last Score' for a different run, not our identity"
        )
    if age < -_RECAPTCHA_FRESHNESS_S:
        # Clock skew is possible, but a far-future timestamp is suspicious.
        raise ValueError(
            f"result line timestamp is {-age:.0f}s in the future — "
            "refusing to trust it (clock skew or cached/forged line)"
        )
    return score


def _resolve_page(page_or_launcher: Any):
    """Return ``(page, closer)`` from either a live Page or a zero-arg launcher.

    ``page_or_launcher`` may be:
      * a Playwright ``Page`` (used directly; we do not close it), or
      * a zero-argument callable that returns a ``Page`` (e.g. a Wraith
        launcher / browser factory); we own and close whatever it returns.

    ``closer`` is a no-arg callable that releases anything we created. We try
    to close the page's owning context/browser when we launched it ourselves,
    and stay hands-off when handed a live page we don't own.
    """
    # A Page exposes goto(); a launcher is just callable. Prefer the Page
    # check first so a Page subclass that is also callable isn't relaunched.
    if hasattr(page_or_launcher, "goto") and callable(getattr(page_or_launcher, "goto")):
        return page_or_launcher, (lambda: None)

    if callable(page_or_launcher):
        page = page_or_launcher()
        if not (hasattr(page, "goto") and callable(getattr(page, "goto"))):
            raise TypeError(
                "launcher did not return a Playwright Page "
                f"(got {type(page).__name__})"
            )

        def _close() -> None:
            # Best-effort teardown of the page and its owning context/browser.
            for closer in (
                getattr(page, "close", None),
                getattr(getattr(page, "context", None), "close", None),
                getattr(
                    getattr(getattr(page, "context", None), "browser", None),
                    "close",
                    None,
                ),
            ):
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        pass

        return page, _close

    raise TypeError(
        "page_or_launcher must be a Playwright Page or a zero-arg callable "
        f"returning a Page (got {type(page_or_launcher).__name__})"
    )


def recaptcha_v3_score(page_or_launcher: Any) -> float:
    """Measure the reCAPTCHA-v3 reputation score for the current identity.

    Navigates the supplied browser to cleantalk's reCAPTCHA-v3 tester, waits
    for the score widget to render, and returns the score as a float in
    ``0.0..1.0``.

    Crucially it parses **only** the canonical
    ``Result: X | Time: ... | Hostname: ...`` line and verifies the line's
    timestamp is fresh — never a generic page-wide regex. See the module-level
    note on ``_RESULT_LINE_RE`` for *why*: the page's FAQ contains a decoy
    ``0.9`` and the tester caches a stale "Last Score"; both produce false
    reads if you scrape the page naively.

    :param page_or_launcher: a live Playwright ``Page``, or a zero-arg
        callable returning one (the latter is launched and torn down here).
    :returns: the score in ``[0.0, 1.0]``. Low (~0.1-0.3) means we look like a
        bot — fall back to identity borrowing; high (~0.9) means warmed.
    :raises ValueError: if no fresh canonical result line can be read.
    """
    page, close = _resolve_page(page_or_launcher)
    try:
        page.goto(RECAPTCHA_V3_TEST_URL, wait_until="networkidle")

        # The score is filled in asynchronously after grecaptcha resolves.
        # Poll the page text for a *fresh* canonical line rather than waiting
        # on a fixed selector (the tester's markup has changed over time, and
        # we must distinguish the fresh line from the cached "Last Score").
        deadline = time.time() + 30.0
        last_err: Exception | None = None
        while time.time() < deadline:
            # Prefer the focused result element if present (less FAQ noise),
            # but fall through to whole-page text so a markup change can't
            # silently break us.
            candidate_texts: list[str] = []
            for sel in _RECAPTCHA_RESULT_SELECTORS:
                try:
                    el = page.query_selector(sel)
                    if el is not None:
                        candidate_texts.append(el.inner_text())
                except Exception:
                    pass
            try:
                candidate_texts.append(page.content())
            except Exception as exc:  # pragma: no cover - transient nav
                last_err = exc

            for text in candidate_texts:
                try:
                    return _extract_fresh_score(text)
                except ValueError as exc:
                    last_err = exc

            page.wait_for_timeout(1000)

        raise ValueError(
            f"timed out waiting for a fresh reCAPTCHA-v3 result line: {last_err}"
        )
    finally:
        close()


# ---------------------------------------------------------------------------
# rebrowser bot-detector
# ---------------------------------------------------------------------------

# The bot-detector exposes one result row per test, each marked id="detections-json"
# in newer builds, or as <p class="detection ..."> rows in older ones. We read
# the structured JSON when available and fall back to scraping the rows. These
# are the tests we care about for the Wraith stealth backends.
_BOT_DETECTOR_TESTS = (
    "runtimeEnableLeak",   # Runtime.enable CDP leak (patchright suppresses this)
    "navigatorWebdriver",  # navigator.webdriver === true
    "viewport",            # default 1280x720 viewport is a red flag; want viewport=None
    "pwInitScripts",       # Playwright init-script / addInitScript leak
    "dummyFn",             # exposeFunction / dummyFn leak
    "sourceUrlLeak",       # //# sourceURL= leak from injected scripts
)

# JS that reads rebrowser-bot-detector's own result table into a flat dict of
# {testName: status}. The detector stores results on window and also renders
# them into the DOM; we read the DOM rows defensively (resilient to the exact
# global it uses) and normalise the status text.
_BOT_DETECTOR_SCRAPE_JS = r"""
() => {
  const out = {};
  // 1) Preferred: a JSON blob the detector renders for programmatic reads.
  const jsonEl = document.querySelector('#detections-json, [data-detections-json]');
  if (jsonEl) {
    try {
      const raw = jsonEl.textContent || jsonEl.getAttribute('data-detections-json');
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        for (const [k, v] of Object.entries(parsed)) {
          out[k] = (v && typeof v === 'object')
            ? (v.status ?? v.result ?? JSON.stringify(v))
            : v;
        }
      }
    } catch (e) { /* fall through to DOM scrape */ }
  }
  // 2) DOM scrape: each test renders a row whose dataset/id names the test
  //    and whose text/class carries pass|fail|warn.
  const rows = document.querySelectorAll(
    '[data-test], [data-detection], .detection, .test-result, tr[id]'
  );
  rows.forEach((row) => {
    const name = row.getAttribute('data-test')
      || row.getAttribute('data-detection')
      || row.id
      || (row.querySelector('[data-test]') || {}).getAttribute?.('data-test');
    if (!name) return;
    let status = (row.getAttribute('data-status') || '').trim();
    if (!status) {
      const cls = (row.className || '').toLowerCase();
      if (cls.includes('fail') || cls.includes('detected') || cls.includes('red')) status = 'fail';
      else if (cls.includes('warn') || cls.includes('yellow')) status = 'warn';
      else if (cls.includes('pass') || cls.includes('ok') || cls.includes('green')) status = 'pass';
    }
    if (!status) status = (row.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 200);
    if (!(name in out)) out[name] = status;
  });
  return out;
}
"""


def bot_detector(page: Any) -> dict:
    """Run rebrowser's bot-detector and return per-test statuses.

    Navigates ``page`` to ``bot-detector.rebrowser.net``, waits for the test
    suite to run, and scrapes each test's status.

    The returned dict always contains keys for the Wraith-relevant tests
    (:data:`_BOT_DETECTOR_TESTS`) — ``runtimeEnableLeak``,
    ``navigatorWebdriver``, ``viewport``, ``pwInitScripts``, ``dummyFn`` and
    ``sourceUrlLeak`` — mapped to whatever status the detector reported
    (typically a ``pass``/``fail``/``warn`` token or descriptive text), with
    ``None`` for any test the detector did not report. Any *additional* tests
    the detector emits are included too.

    :param page: a live Playwright ``Page`` (driven by the stealth backend you
        want to assess). Use ``viewport=None`` and the patchright backend to
        clear ``viewport`` and ``runtimeEnableLeak`` respectively.
    :returns: ``{testName: status}``.
    """
    page.goto(BOT_DETECTOR_URL, wait_until="networkidle")

    # Some detections (notably runtimeEnableLeak) only fire on the *next* CDP
    # Runtime.enable, which the detector triggers shortly after load. Give the
    # suite a moment and poll until at least one known test reports.
    results: dict[str, Any] = {}
    deadline = time.time() + 20.0
    while time.time() < deadline:
        try:
            scraped = page.evaluate(_BOT_DETECTOR_SCRAPE_JS)
        except Exception:
            scraped = {}
        if isinstance(scraped, dict):
            results.update({k: v for k, v in scraped.items() if v not in (None, "")})
        if any(t in results for t in _BOT_DETECTOR_TESTS):
            break
        page.wait_for_timeout(1000)

    # Guarantee the Wraith-relevant keys exist so callers can assert on them.
    return {test: results.get(test) for test in _BOT_DETECTOR_TESTS} | {
        k: v for k, v in results.items() if k not in _BOT_DETECTOR_TESTS
    }


# ---------------------------------------------------------------------------
# WAAP / anti-bot vendor fingerprinting
# ---------------------------------------------------------------------------

# Vendor signatures, checked against the response's headers, set cookies,
# status code and (when available) JS-rendered globals.
#
# Reblaze / Link11 is the one we care most about: no public bypass exists for
# it (even commercial multi-WAF SDKs skip it). Tells, all empirically seen on
# EL AL's stack:
#   * Server header literally "rhino-core-shield"
#   * cookies waap_id and/or rbzid
#   * non-standard statuses 247 (JS challenge), 248 (token exchange),
#     492 (hard block — e.g. UA contains "HeadlessChrome")
#   * challenge page sets window.rbzns = {seed, bereshit:'1'} and calls
#     winsocks()
#
# (ac_v2 detail, for the strategist: it's a one-shot fingerprint + seed-keyed
# SHA-1 hashcash with NO behavioral tracking — hence the engine-choice +
# identity-borrowing approach beats it where solvers can't.)


def _vendor_checks(
    headers: Mapping[str, str],
    cookies: Mapping[str, str],
    status: int | None,
    body: str | None,
) -> list[str]:
    """Pure mapping of normalised signals -> vendor names. Order = stable."""
    # Normalise: lowercase header names and cookie names for case-insensitive
    # matching; keep values as-is (some matches are substring on value).
    h = {k.lower(): (v or "") for k, v in headers.items()}
    hvals = " ".join(h.values()).lower()
    ck = {k.lower(): (v or "") for k, v in cookies.items()}
    b = (body or "").lower()

    def has_header(name: str) -> bool:
        return name.lower() in h

    def header_contains(name: str, needle: str) -> bool:
        return needle.lower() in h.get(name.lower(), "").lower()

    def any_header_contains(needle: str) -> bool:
        return needle.lower() in hvals or any(
            needle.lower() in k for k in h
        )

    def has_cookie(name: str) -> bool:
        return name.lower() in ck

    found: list[str] = []

    # --- Reblaze / Link11 -------------------------------------------------
    reblaze = (
        header_contains("server", "rhino-core-shield")
        or any_header_contains("rhino-core-shield")
        or has_cookie("waap_id")
        or has_cookie("rbzid")
        or (status in (247, 248, 492))
        or ("window.rbzns" in b)
        or ("bereshit" in b)
        or ("winsocks" in b)
    )
    if reblaze:
        found.append("Reblaze/Link11")

    # --- Akamai -----------------------------------------------------------
    akamai = (
        has_cookie("aka_a2")
        or has_cookie("_abck")
        or has_cookie("bm_sz")
        or has_cookie("ak_bmsc")
        or has_header("x-akamai-transformed")
        or any_header_contains("akamaighost")
        or header_contains("server", "akamai")
    )
    if akamai:
        found.append("Akamai")

    # --- reCAPTCHA --------------------------------------------------------
    recaptcha = (
        "grecaptcha" in b
        or "recaptcha/api.js" in b
        or "www.google.com/recaptcha" in b
        or "g-recaptcha" in b
    )
    if recaptcha:
        found.append("reCAPTCHA")

    # --- DataDome ---------------------------------------------------------
    datadome = (
        has_cookie("datadome")
        or has_header("x-datadome")
        or has_header("x-datadome-cid")
        or any_header_contains("datadome")
    )
    if datadome:
        found.append("DataDome")

    # --- Incapsula / Imperva ---------------------------------------------
    incapsula = (
        has_cookie("visid_incap")
        or has_cookie("reese84")
        or any(k.startswith("incap_ses") for k in ck)
        or any(k.startswith("visid_incap") for k in ck)
        or any_header_contains("incapsula")
        or header_contains("x-cdn", "incapsula")
        or has_header("x-iinfo")  # Imperva info header
    )
    if incapsula:
        found.append("Incapsula/Imperva")

    # --- SiteMinder (CA SSO) ---------------------------------------------
    siteminder = (
        has_cookie("smsession")
        or has_cookie("smidentity")
        or "/siteminderagent/" in b
        or any_header_contains("siteminder")
    )
    if siteminder:
        found.append("SiteMinder")

    return found


def _cookies_from_headers(headers: Mapping[str, str], raw_set_cookie: Any = None) -> dict:
    """Extract cookie *names* from Set-Cookie headers (values not needed)."""
    cookies: dict[str, str] = {}

    def _ingest(line: str) -> None:
        # "name=value; Path=/; HttpOnly" -> name, value
        first = line.split(";", 1)[0].strip()
        if "=" in first:
            name, _, value = first.partition("=")
            name = name.strip()
            if name:
                cookies[name] = value.strip()

    # httpx exposes multiple Set-Cookie via headers.get_list; a plain mapping
    # collapses them. Accept an explicit list too.
    if raw_set_cookie:
        items = raw_set_cookie if isinstance(raw_set_cookie, (list, tuple)) else [raw_set_cookie]
        for line in items:
            _ingest(str(line))
    sc = headers.get("set-cookie") if hasattr(headers, "get") else None
    if sc:
        # A single mapping value may itself contain several joined by ", " —
        # but commas appear inside Expires dates, so only split on the safe
        # ", name=" boundary.
        for part in re.split(r",\s*(?=[^=;,\s]+=)", sc):
            _ingest(part)
    return cookies


def identify_waap(url_or_response: Union[str, httpx.Response, Any]) -> list[str]:
    """Fingerprint the WAAP/anti-bot vendor(s) in front of a target.

    Accepts either:
      * a URL string — an httpx GET is issued (redirects followed, browser-ish
        UA) and its headers/cookies/status/body are analysed; or
      * an ``httpx.Response`` — analysed directly (no network call); or
      * a Playwright ``Response`` — its headers, status and body are analysed,
        plus the owning page's cookies if reachable.

    Returns a de-duplicated, stably-ordered list of detected vendor names,
    drawn from: ``Reblaze/Link11``, ``Akamai``, ``reCAPTCHA``, ``DataDome``,
    ``Incapsula/Imperva``, ``SiteMinder``. Empty list means none recognised.

    Detection is header/cookie/status driven (see :func:`_vendor_checks`); for
    a URL or httpx response we also scan the body for JS tells (grecaptcha,
    window.rbzns/winsocks/bereshit, /siteminderagent/). JS-only globals that
    appear only after rendering are best detected by passing a Playwright
    response or by combining with :func:`bot_detector`'s page.
    """
    headers: Mapping[str, str]
    cookies: dict[str, str]
    status: int | None
    body: str | None

    if isinstance(url_or_response, str):
        with httpx.Client(
            follow_redirects=True,
            timeout=20.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        ) as client:
            resp = client.get(url_or_response)
        headers = resp.headers
        cookies = _cookies_from_headers(
            resp.headers,
            resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else None,
        )
        status = resp.status_code
        body = _safe_text(resp)

    elif isinstance(url_or_response, httpx.Response):
        resp = url_or_response
        headers = resp.headers
        cookies = _cookies_from_headers(
            resp.headers,
            resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else None,
        )
        status = resp.status_code
        body = _safe_text(resp)

    else:
        # Treat as a Playwright Response (duck-typed: has .headers and .status).
        resp = url_or_response
        try:
            raw_headers = resp.headers  # dict in Playwright
        except Exception as exc:  # pragma: no cover - bad input
            raise TypeError(
                "identify_waap expects a URL str, httpx.Response, or "
                f"Playwright Response (got {type(url_or_response).__name__})"
            ) from exc
        headers = raw_headers if isinstance(raw_headers, Mapping) else dict(raw_headers)
        # Playwright merges Set-Cookie into the headers dict (comma-joined).
        cookies = _cookies_from_headers(headers)
        # Augment with the real cookie jar from the owning context if reachable.
        cookies.update(_playwright_response_cookies(resp))
        status = getattr(resp, "status", None)
        if callable(status):  # some bindings expose status() as a method
            try:
                status = status()
            except Exception:
                status = None
        body = _playwright_response_body(resp)

    return _vendor_checks(headers, cookies, status, body)


def _safe_text(resp: httpx.Response) -> str | None:
    try:
        return resp.text
    except Exception:
        try:
            return resp.content.decode("utf-8", "ignore")
        except Exception:
            return None


def _playwright_response_body(resp: Any) -> str | None:
    for attr in ("text", "body"):
        fn = getattr(resp, attr, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, bytes):
                    return out.decode("utf-8", "ignore")
                return out
            except Exception:
                continue
    return None


def _playwright_response_cookies(resp: Any) -> dict:
    try:
        frame = resp.frame
        page = frame.page
        context = page.context
        jar = context.cookies()
        return {c["name"]: c.get("value", "") for c in jar if "name" in c}
    except Exception:
        return {}
