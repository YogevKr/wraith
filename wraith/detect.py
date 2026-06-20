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

The WAAP layer is driven by a single source of truth, :data:`SIGNATURES`, a
table of vendor signatures (response headers, set-cookie/cookie names,
script-src hosts, in-page JS globals, status codes). On top of it sit:

* :func:`identify_waap` — back-compatible ``list[str]`` of vendor names;
* :func:`fingerprint` — a richer structured ``dict`` (per-vendor tier,
  strategy, evidence, clearance cookies);
* :data:`CLEARANCE_COOKIES` — ``{vendor: [cookie names]}`` whose union is the
  default set of cookies the engine polls for when clearing a challenge;
* :func:`cookie_is_valid` — whether a clearance cookie's *value* actually
  represents a solved/cleared state (the Akamai ``_abck`` ``~0~``/``~-1~``
  subtlety lives here).

httpx is used for cheap header-only probes; Playwright is used wherever the
signal only exists after JavaScript runs (the reCAPTCHA score and the
bot-detector results are both JS-rendered).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Union

import httpx

log = logging.getLogger("wraith.detect")

__all__ = [
    "RECAPTCHA_V3_TEST_URL",
    "BOT_DETECTOR_URL",
    "recaptcha_v3_score",
    "recaptcha_params",
    "bot_detector",
    "identify_waap",
    "fingerprint",
    "Signature",
    "SIGNATURES",
    "CLEARANCE_COOKIES",
    "cookie_is_valid",
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
# reCAPTCHA-v3 parameter probe (sitekey / action / enterprise / host)
# ---------------------------------------------------------------------------
#
# Where recaptcha_v3_score answers "what reputation does *this identity* get",
# recaptcha_params answers the orthogonal "what reCAPTCHA is this page running"
# — the facts a token harvester / reputation-borrowing flow needs before it can
# act: the sitekey, the action label(s), whether it is the *enterprise* product,
# and which host serves the iframe.
#
# It is a LAYERED probe because no single source is reliable on its own:
#
#   1. window.___grecaptcha_cfg.clients — grecaptcha's own internal client
#      registry. This is the ground truth when present: each client carries the
#      sitekey it was rendered with, and the client-id (cid) encodes the
#      version: v2 widgets get small integer cids (0, 1, ...), while v3
#      (render=...) clients are allocated ids >= 10000. We walk the (deeply
#      nested, minified, version-varying) client objects defensively, pulling
#      any 'sitekey' and any 'action' we can find and noting whether any cid is
#      a v3 id.
#   2. DOM/script fallbacks — if cfg isn't populated yet (script still loading,
#      or an explicitly-rendered widget) we fall back to: data-sitekey on any
#      element, the .g-recaptcha container's data-sitekey, the iframe ?k=
#      param, and the api.js?render=<sitekey> query param.
#   3. Enterprise detection — the enterprise product uses a different JS object
#      (grecaptcha.enterprise), a different loader (enterprise.js), and a
#      different verification backend (recaptchaenterprise.googleapis.com). Any
#      of these flips enterprise=True. This matters because enterprise tokens
#      are verified server-side against a Google Cloud project and may *ignore*
#      borrowed .google.com reputation cookies.
#   4. Host — the iframe is normally served from www.google.com (reads the
#      .google.com reputation cookies). Some sites load it from
#      www.recaptcha.net instead (the China-friendly mirror) which does NOT see
#      .google.com cookies — so reputation borrowing silently fails there. We
#      surface the host and warn on recaptcha.net.
#
# The probe is wholly defensive: every JS read is wrapped, every layer has a
# fallback, and a page with no reCAPTCHA at all yields version "none" rather
# than raising. The returned object is a RecaptchaParams (imported lazily from
# wraith.recaptcha_v3 to avoid an import cycle; a duck-typed local shim is used
# if that module isn't importable yet, so detect.py always imports cleanly).

# v3 (render=...) grecaptcha clients are allocated client-ids at/above this
# threshold; v2 widgets get small sequential ids (0, 1, ...).
_RECAPTCHA_V3_CID_THRESHOLD = 10000

# The canonical host that serves the reCAPTCHA iframe and reads .google.com
# reputation cookies; the .net mirror does not, so borrowing fails there.
_RECAPTCHA_GOOGLE_HOST = "www.google.com"
_RECAPTCHA_NET_HOST = "www.recaptcha.net"

# In-page probe that mines grecaptcha's internal client registry plus the DOM /
# script tags for the sitekey, action(s), version, enterprise flag and iframe
# host. Returns a plain JSON-able object; every access is guarded so a hostile
# or half-loaded page can't throw out of evaluate().
_RECAPTCHA_PARAMS_PROBE = r"""
() => {
  const out = {
    present: false,
    enterprise: false,
    sitekeys: [],
    actions: [],
    v3_cid: false,
    v2_cid: false,
    hosts: [],
    loader_enterprise: false,
    backend_enterprise: false,
  };
  const SITEKEY_RE = /^[A-Za-z0-9_-]{30,60}$/;
  const addKey = (k) => {
    if (typeof k === 'string' && SITEKEY_RE.test(k) && !out.sitekeys.includes(k)) {
      out.sitekeys.push(k);
    }
  };
  const addAction = (a) => {
    if (typeof a === 'string' && a && !out.actions.includes(a)) out.actions.push(a);
  };

  // ---- Layer 1: window.___grecaptcha_cfg.clients ------------------------
  try {
    const cfg = window.___grecaptcha_cfg;
    if (cfg && cfg.clients && typeof cfg.clients === 'object') {
      out.present = true;
      for (const cid of Object.keys(cfg.clients)) {
        const idnum = parseInt(cid, 10);
        if (!isNaN(idnum)) {
          if (idnum >= 10000) out.v3_cid = true; else out.v2_cid = true;
        }
        // The client object is deeply/minified-nested; BFS for sitekey/action.
        const root = cfg.clients[cid];
        const seen = new Set();
        const stack = [root];
        let budget = 5000;  // bounded traversal — minified graphs can cycle.
        while (stack.length && budget-- > 0) {
          const node = stack.pop();
          if (!node || typeof node !== 'object' || seen.has(node)) continue;
          seen.add(node);
          for (const key of Object.keys(node)) {
            let val;
            try { val = node[key]; } catch (e) { continue; }
            const lk = key.toLowerCase();
            if (typeof val === 'string') {
              if (lk === 'sitekey' || lk === 'k') addKey(val);
              if (lk === 'action') addAction(val);
            } else if (val && typeof val === 'object') {
              stack.push(val);
            }
          }
        }
      }
    }
  } catch (e) { /* fall through to DOM/script fallbacks */ }

  // ---- Layer 2: DOM / script fallbacks ----------------------------------
  try {
    // data-sitekey on .g-recaptcha (and any element that carries it).
    document.querySelectorAll('[data-sitekey]').forEach((el) => {
      out.present = true;
      addKey(el.getAttribute('data-sitekey'));
      const act = el.getAttribute('data-action');
      if (act) addAction(act);
    });
    if (document.querySelector('.g-recaptcha, .grecaptcha-badge')) out.present = true;

    // reCAPTCHA iframe(s): host + ?k=<sitekey> param.
    document.querySelectorAll('iframe[src*="recaptcha"]').forEach((f) => {
      out.present = true;
      let u;
      try { u = new URL(f.src, location.href); } catch (e) { return; }
      if (u.hostname && !out.hosts.includes(u.hostname)) out.hosts.push(u.hostname);
      const k = u.searchParams.get('k');
      if (k) addKey(k);
    });

    // Loader script: api.js?render=<sitekey> (v3) and enterprise.js (ent).
    document.querySelectorAll('script[src]').forEach((s) => {
      const src = s.src || '';
      if (!/recaptcha/i.test(src)) return;
      out.present = true;
      let u;
      try { u = new URL(src, location.href); } catch (e) { return; }
      if (u.hostname && !out.hosts.includes(u.hostname)) out.hosts.push(u.hostname);
      if (/enterprise\.js/i.test(u.pathname)) out.loader_enterprise = true;
      const render = u.searchParams.get('render');
      if (render && render !== 'explicit' && render !== 'onload') addKey(render);
    });
  } catch (e) { /* defensive */ }

  // ---- Layer 3: enterprise detection ------------------------------------
  try {
    if (window.grecaptcha && window.grecaptcha.enterprise) {
      out.enterprise = true;
      out.present = true;
    }
  } catch (e) { /* defensive */ }
  out.enterprise = out.enterprise || out.loader_enterprise;

  // recaptchaenterprise.googleapis.com referenced anywhere in inline scripts.
  try {
    for (const s of document.scripts) {
      const t = s.textContent || '';
      if (t.indexOf('recaptchaenterprise.googleapis.com') !== -1) {
        out.backend_enterprise = true;
        out.enterprise = true;
        break;
      }
    }
  } catch (e) { /* defensive */ }

  return out;
}
"""


def _resolve_recaptcha_params_cls():
    """Return the ``RecaptchaParams`` class to instantiate.

    Imported lazily from :mod:`wraith.recaptcha_v3` to avoid an import cycle
    (recaptcha_v3 imports detect). If that module is not importable yet (e.g.
    during a partial build), fall back to a local duck-typed dataclass with the
    identical field shape so ``detect.py`` always imports and runs cleanly.
    """
    try:
        from wraith.recaptcha_v3 import RecaptchaParams  # type: ignore

        return RecaptchaParams
    except Exception:
        return _RecaptchaParamsShim


@dataclass
class _RecaptchaParamsShim:
    """Duck-typed stand-in for :class:`wraith.recaptcha_v3.RecaptchaParams`.

    Same field shape (``version, enterprise, sitekey, actions, host``) so the
    two are interchangeable for callers. Used only when recaptcha_v3 cannot be
    imported (keeps detect.py self-contained / cycle-free).
    """

    version: str
    enterprise: bool
    sitekey: str
    actions: list
    host: str


def recaptcha_params(page: Any) -> Any:
    """Probe a live page for the reCAPTCHA it runs (sitekey/action/version/host).

    This is the *configuration* counterpart to :func:`recaptcha_v3_score`: it
    reports what reCAPTCHA the page is wired to, not what score the current
    identity earns. A reputation-borrowing / token-harvesting flow needs both.

    Layered, defensive probe (see the module note above for the full rationale):

      1. ``window.___grecaptcha_cfg.clients`` — grecaptcha's own client
         registry is ground truth when present; client-ids ``>= 10000`` mark a
         v3 (``render=...``) client, smaller ids mark v2 widgets. We BFS each
         (minified, version-varying) client object for ``sitekey``/``action``.
      2. DOM/script fallbacks — ``data-sitekey`` (incl. ``.g-recaptcha``), the
         reCAPTCHA ``iframe`` ``?k=`` param, and ``api.js?render=<sitekey>``.
      3. Enterprise — ``grecaptcha.enterprise`` object, ``enterprise.js``
         loader, or a ``recaptchaenterprise.googleapis.com`` reference flips
         ``enterprise=True`` (enterprise tokens are verified against a Cloud
         project and may ignore borrowed ``.google.com`` cookies).
      4. Host — the iframe host (``www.google.com`` vs the ``www.recaptcha.net``
         mirror). We **warn** on ``www.recaptcha.net`` because it does not read
         ``.google.com`` reputation cookies, so identity-borrowing silently
         fails there.

    Version is resolved as: ``"v3"`` if a v3 client-id is present or only a
    ``render=`` sitekey (no v2 widget) was found; ``"v2"`` if a v2 widget/cid
    is present; ``"enterprise"`` if enterprise was detected (orthogonal to v2/v3
    but reported in the ``version`` field as the dominant fact, with the
    ``enterprise`` flag also set); ``"none"`` if no reCAPTCHA is detected at all.

    :param page: a live Playwright ``Page`` (must have already navigated to the
        target so grecaptcha has had a chance to load).
    :returns: a :class:`wraith.recaptcha_v3.RecaptchaParams` (imported lazily; a
        local duck-typed shim with the same fields is returned if that module is
        not importable yet). On a page with no reCAPTCHA, ``version="none"``,
        ``sitekey=""``, ``actions=[]``, ``host=""``, ``enterprise=False``.
    """
    cls = _resolve_recaptcha_params_cls()

    probe: dict[str, Any] = {}
    try:
        result = page.evaluate(_RECAPTCHA_PARAMS_PROBE)
        if isinstance(result, dict):
            probe = result
    except Exception as exc:
        # A page that can't be evaluated (closed, cross-origin nav in flight)
        # is treated as "no reCAPTCHA detected" rather than raising.
        log.debug("recaptcha_params: page.evaluate failed: %s", exc)
        probe = {}

    sitekeys = [s for s in probe.get("sitekeys", []) if isinstance(s, str) and s]
    actions = [a for a in probe.get("actions", []) if isinstance(a, str) and a]
    hosts = [h for h in probe.get("hosts", []) if isinstance(h, str) and h]
    enterprise = bool(probe.get("enterprise"))
    v3_cid = bool(probe.get("v3_cid"))
    v2_cid = bool(probe.get("v2_cid"))
    present = bool(probe.get("present")) or bool(sitekeys) or v3_cid or v2_cid

    if not present:
        return cls(version="none", enterprise=False, sitekey="", actions=[], host="")

    # Host resolution: prefer a real reCAPTCHA host; warn on the .net mirror.
    host = ""
    recaptcha_hosts = [
        h for h in hosts if "google.com" in h or "recaptcha.net" in h
    ]
    if recaptcha_hosts:
        # Prefer www.google.com if present; else take the first (likely .net).
        google = next((h for h in recaptcha_hosts if h == _RECAPTCHA_GOOGLE_HOST), None)
        host = google or recaptcha_hosts[0]
    elif hosts:
        host = hosts[0]

    if host == _RECAPTCHA_NET_HOST or (host and "recaptcha.net" in host):
        log.warning(
            "recaptcha_params: reCAPTCHA iframe served from %s (recaptcha.net "
            "mirror) — it does NOT read .google.com reputation cookies, so "
            "identity-borrowing will silently fail against this site.",
            host,
        )

    # Version resolution. v3 if a v3 client-id is present, OR a sitekey was
    # found with no v2 widget in evidence (render=<key> flows are v3). v2 if a
    # v2 client/widget is present. Enterprise is reported in the version field
    # as the dominant fact (the enterprise flag is also set independently).
    if enterprise:
        version = "enterprise"
    elif v3_cid:
        version = "v3"
    elif v2_cid:
        version = "v2"
    elif sitekeys:
        # Sitekey found via render=/iframe but no cfg client classified it.
        version = "v3"
    else:
        # reCAPTCHA detected (e.g. a bare .g-recaptcha container) but no
        # sitekey/version evidence yet — call it v2 (the explicit-render case).
        version = "v2"

    sitekey = sitekeys[0] if sitekeys else ""

    return cls(
        version=version,
        enterprise=enterprise,
        sitekey=sitekey,
        actions=actions,
        host=host,
    )


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
# WAAP / anti-bot vendor fingerprinting — the signature table
# ---------------------------------------------------------------------------
#
# `SIGNATURES` is the single source of truth for vendor detection AND for the
# clearance specs the engine consumes. Each `Signature` declares the signals
# that prove a vendor is present, plus how hard it is to get past and which
# cookies (if any) mark a cleared session.
#
# Matching is intentionally cheap and offline: we only look at response headers,
# Set-Cookie / cookie names, the response body (script-src hosts and in-page JS
# globals show up as substrings there) and the status code. JS-only globals that
# appear only after rendering are best detected by passing a Playwright page (see
# `fingerprint`), but most also leave a substring tell in the served HTML.
#
# Tiers (how the rest of Wraith should treat the vendor):
#   1 — engine choice alone usually passes (a good stealth browser clears it).
#   2 — needs a smarter approach (behavioral nudge, warmed identity, retries).
#   3 — a human-grade solver / CAPTCHA or IAM credential is required; no
#       cookie-poll clearance is possible from automation alone.
#
# Reblaze / Link11 is the one we care most about: no public bypass exists for
# it (even commercial multi-WAF SDKs skip it). Tells, all empirically seen on
# EL AL's stack:
#   * Server header literally "rhino-core-shield"
#   * cookies waap_id and/or rbzid
#   * non-standard statuses 247 (JS challenge), 248 (token exchange),
#     474/481 (IP rate-limit), 492 (hard block — e.g. UA "HeadlessChrome")
#   * challenge page sets window.rbzns = {seed, bereshit:'1'} and calls
#     winsocks()
#
# (ac_v2 detail, for the strategist: it's a one-shot fingerprint + seed-keyed
# SHA-1 hashcash with NO behavioral tracking — hence the engine-choice +
# identity-borrowing approach beats it where solvers can't.)


@dataclass(frozen=True)
class Signature:
    """One vendor's detection signature + clearance spec.

    Detection is the OR of every populated signal field below; the first that
    matches flags the vendor. All matching is case-insensitive.

    :param name: canonical vendor name (the string ``identify_waap`` returns).
    :param tier: 1 (engine passes) / 2 (smarter) / 3 (solver/IAM needed).
    :param strategy: a short human hint on how to get past this vendor.
    :param clearance_cookies: cookies whose presence (and, per
        :func:`cookie_is_valid`, value) marks a cleared session. May be empty
        for IAM gateways and pure-CAPTCHA vendors where no cookie clears.
    :param headers: header names whose mere presence flags the vendor.
    :param header_contains: ``{header: substring}`` value matches.
    :param server: substrings to look for in the ``Server`` header.
    :param header_substr_any: substrings matched against ANY header name OR
        value (catches vendor names sprayed across odd custom headers).
    :param cookies: cookie names whose presence flags the vendor.
    :param cookie_prefixes: cookie-name prefixes (e.g. ``visid_incap``) — match
        any cookie whose name starts with one of these.
    :param body: substrings to look for in the response body (script-src hosts
        and in-page JS globals show up here).
    :param statuses: non-standard status codes that flag the vendor.
    """

    name: str
    tier: int
    strategy: str
    clearance_cookies: tuple[str, ...] = ()
    headers: tuple[str, ...] = ()
    header_contains: tuple[tuple[str, str], ...] = ()
    server: tuple[str, ...] = ()
    header_substr_any: tuple[str, ...] = ()
    cookies: tuple[str, ...] = ()
    cookie_prefixes: tuple[str, ...] = ()
    body: tuple[str, ...] = ()
    statuses: tuple[int, ...] = ()


# Order matters: it determines the stable order of `identify_waap`'s output.
# Reblaze first (our headline target), then the heavyweight WAAPs, then the
# CDN/edge ones, then the CAPTCHA widgets and IAM gateways.
SIGNATURES: tuple[Signature, ...] = (
    Signature(
        name="Reblaze/Link11",
        tier=3,  # ac_v2 one-shot hashcash; no public solver — engine + identity.
        strategy="camoufox/firefox engine + warmed identity; ac_v2 has no public solver",
        clearance_cookies=("waap_id", "rbzid"),
        server=("rhino-core-shield",),
        header_substr_any=("rhino-core-shield",),
        cookies=("waap_id", "rbzid"),
        body=("window.rbzns", "bereshit", "winsocks"),
        statuses=(247, 248, 474, 481, 492),
    ),
    Signature(
        name="Akamai",
        tier=2,  # behavioral + fingerprint; needs a nudge + warmed session.
        strategy="behavioral nudge + warmed identity; _abck must reach the ~0~ solved state",
        clearance_cookies=("_abck", "bm_sz"),
        headers=("x-akamai-transformed",),
        server=("akamaighost", "akamai"),
        header_substr_any=("akamaighost",),
        cookies=("_abck", "bm_sz", "ak_bmsc", "bm_sv", "aka_a2"),
        body=("akam",),
    ),
    Signature(
        name="DataDome",
        tier=2,  # fingerprint + behavioral; device-check/CAPTCHA interstitial.
        strategy="behavioral nudge + warmed identity; may serve a captcha-delivery interstitial",
        clearance_cookies=("datadome",),
        headers=("x-datadome", "x-datadome-cid", "x-dd-b"),
        header_substr_any=("datadome",),
        cookies=("datadome",),
        body=("js.datadome.co", "captcha-delivery.com", "datadome"),
    ),
    Signature(
        name="PerimeterX/HUMAN",
        tier=2,  # PX/HUMAN behavioral scoring; needs a nudge + warmed session.
        strategy="behavioral nudge + warmed identity; HUMAN scores continuously",
        clearance_cookies=("_px", "_px2", "_px3"),
        headers=("x-px",),
        cookies=("_px", "_px2", "_px3", "_pxhd", "_pxvid", "_pxde"),
        body=("client.perimeterx.net", "perimeterx", "px-captcha", "_pxAppId"),
    ),
    Signature(
        name="Kasada",
        tier=3,  # ips.js POST-to-/tl token exchange; no public cookie clearance.
        strategy="no public bypass; requires solving the ips.js /tl telemetry exchange",
        clearance_cookies=(),
        headers=("x-kpsdk-ct", "x-kpsdk-cd", "x-kpsdk-r", "x-kpsdk-v"),
        header_substr_any=("kpsdk",),
        body=("/ips.js", "kpsdk", "KPSDK"),
    ),
    Signature(
        name="Imperva/Incapsula",
        tier=2,  # reese84 JS challenge; interstitial "Incapsula incident ID".
        strategy="solve the reese84 JS challenge via a real engine; warmed identity helps",
        clearance_cookies=("visid_incap", "incap_ses", "reese84"),
        headers=("x-iinfo",),
        header_contains=(("x-cdn", "incapsula"),),
        header_substr_any=("incapsula",),
        cookies=("reese84",),
        cookie_prefixes=("visid_incap", "incap_ses", "nlbi_"),
        body=("/_incapsula_resource", "incapsula incident", "incapsula"),
    ),
    Signature(
        name="Cloudflare",
        tier=2,  # Turnstile / "Just a moment" JS challenge; cf_clearance clears it.
        strategy="real engine clears the JS challenge; Turnstile may need a solver",
        clearance_cookies=("cf_clearance",),
        headers=("cf-ray", "cf-mitigated"),
        server=("cloudflare",),
        cookies=("cf_clearance", "__cf_bm"),
        body=(
            "just a moment",
            "challenges.cloudflare.com",
            "cf-turnstile",
            "/cdn-cgi/challenge-platform",
        ),
    ),
    Signature(
        name="AWS WAF",
        tier=2,  # token-based WAF; aws-waf-token cookie marks a cleared session.
        strategy="real engine solves the WAF challenge to mint aws-waf-token",
        clearance_cookies=("aws-waf-token",),
        headers=("x-amzn-waf-action",),
        header_substr_any=("x-amzn-waf-",),
        cookies=("aws-waf-token",),
        body=("token.awswaf.com", "challenge.js"),
    ),
    Signature(
        name="F5 BIG-IP/Shape",
        tier=2,  # F5/Shape bot defense; opaque TS* tokens, BIGipServer* persistence.
        strategy="real engine + warmed identity; Shape (F5 Distributed Cloud) scores behavior",
        clearance_cookies=(),
        cookie_prefixes=("BIGipServer", "TS"),
        body=("/TSPD/", "BIGipServer"),
    ),
    Signature(
        name="reCAPTCHA",
        tier=3,  # v2 needs a solver; v3 is a reputation score with NO solver.
        strategy="v3 has no solver — borrow a warmed identity (see recaptcha_v3_score); v2 needs a human/solver",
        clearance_cookies=(),
        body=(
            "grecaptcha",
            "recaptcha/api.js",
            "www.google.com/recaptcha",
            "google.com/recaptcha",
            "g-recaptcha",
        ),
    ),
    Signature(
        name="hCaptcha",
        tier=3,  # interactive CAPTCHA; needs a human-grade solver.
        strategy="interactive CAPTCHA — needs a human-grade solver or warmed identity",
        clearance_cookies=(),
        body=("hcaptcha.com", "h-captcha", "js.hcaptcha.com"),
    ),
    Signature(
        name="SiteMinder",
        tier=3,  # CA/Broadcom IAM SSO gateway; auth, not a bot challenge — no cookie poll clears it.
        strategy="IAM SSO gateway — needs valid credentials, not a bot bypass",
        clearance_cookies=(),
        cookies=("smsession", "smidentity", "smchallenge"),
        header_substr_any=("siteminder",),
        body=("/siteminderagent/", "siteminder"),
    ),
)


def _signatures_by_name() -> dict[str, Signature]:
    return {sig.name: sig for sig in SIGNATURES}


# ``{vendor: [clearance cookie names]}`` — built straight from SIGNATURES so the
# table stays the single source of truth. The union of every value is the
# default set of cookies the engine polls for when clearing a challenge.
CLEARANCE_COOKIES: dict[str, list[str]] = {
    sig.name: list(sig.clearance_cookies)
    for sig in SIGNATURES
    if sig.clearance_cookies
}


# ---------------------------------------------------------------------------
# Clearance-cookie validity
# ---------------------------------------------------------------------------

def cookie_is_valid(name: str, value: str) -> bool:
    """Is a clearance cookie's *value* actually in a cleared/solved state?

    For most WAAP clearance cookies presence is enough: if the defense set the
    cookie at all, the session is (or is about to be) cleared, so any non-empty
    value counts as valid.

    SPECIAL CASE — Akamai ``_abck``: presence is NOT enough. Akamai sets an
    ``_abck`` cookie *immediately*, long before the Bot Manager has decided you
    are human. The cookie's value carries the verdict in a ``~N~``-delimited
    field:

      * a fresh / unsolved ``_abck`` contains ``~-1~``  (sensor not yet
        accepted — you are still being challenged), e.g.
        ``...~-1~-1~-1`` ;
      * a *solved* ``_abck`` contains ``~0~`` and NOT ``~-1~`` (sensor accepted
        — you have cleared), e.g. ``...~0~-1~...`` only flips to all-zero once
        cleared, so we require ``~0~`` present AND ``~-1~`` absent.

    So we treat ``_abck`` as valid only when its value contains ``~0~`` and does
    NOT contain ``~-1~``. This is why the engine must keep polling after the
    first ``_abck`` appears: the initial one is the ``~-1~`` unsolved form and
    must not be counted as a pass.

    :param name: cookie name (case-insensitive for the special cases).
    :param value: cookie value as stored in the jar.
    :returns: True if the cookie value represents a cleared/solved session.
    """
    if value is None:
        return False
    v = str(value).strip()
    if not v:
        return False

    lname = name.lower()

    # Akamai _abck: solved only once the value reaches the ~0~ state and no
    # longer carries the ~-1~ unsolved marker.
    if lname == "_abck":
        return ("~0~" in v) and ("~-1~" not in v)

    # Everything else: presence (non-empty value) == valid.
    return True


# ---------------------------------------------------------------------------
# Signal extraction (shared by identify_waap / fingerprint)
# ---------------------------------------------------------------------------

@dataclass
class _Signals:
    """Normalised signals pulled off a target, ready for signature matching."""

    headers: dict[str, str] = field(default_factory=dict)  # lower-name -> value
    cookies: dict[str, str] = field(default_factory=dict)  # name -> value
    status: int | None = None
    body: str = ""
    url: str | None = None

    # Pre-computed lowercased helpers.
    _header_blob: str = ""
    _cookies_lower: dict[str, str] = field(default_factory=dict)
    _body_lower: str = ""

    def finalize(self) -> "_Signals":
        self.headers = {k.lower(): (v or "") for k, v in self.headers.items()}
        self._header_blob = " ".join(
            f"{k} {v}" for k, v in self.headers.items()
        ).lower()
        self._cookies_lower = {k.lower(): (v or "") for k, v in self.cookies.items()}
        self._body_lower = (self.body or "").lower()
        return self


def _match_signature(sig: Signature, sig_signals: _Signals) -> list[str]:
    """Return the list of evidence strings proving ``sig`` matched (empty=no match)."""
    h = sig_signals.headers
    hblob = sig_signals._header_blob
    ck = sig_signals._cookies_lower
    b = sig_signals._body_lower
    evidence: list[str] = []

    for name in sig.headers:
        if name.lower() in h:
            evidence.append(f"header:{name}")

    for name, needle in sig.header_contains:
        if needle.lower() in h.get(name.lower(), "").lower():
            evidence.append(f"header:{name}~={needle}")

    for needle in sig.server:
        if needle.lower() in h.get("server", "").lower():
            evidence.append(f"server~={needle}")

    for needle in sig.header_substr_any:
        if needle.lower() in hblob:
            evidence.append(f"header*~={needle}")

    for name in sig.cookies:
        if name.lower() in ck:
            evidence.append(f"cookie:{name}")

    for prefix in sig.cookie_prefixes:
        pl = prefix.lower()
        if any(k.startswith(pl) for k in ck):
            evidence.append(f"cookie:{prefix}*")

    for needle in sig.body:
        if needle.lower() in b:
            evidence.append(f"body~={needle}")

    if sig_signals.status is not None and sig_signals.status in sig.statuses:
        evidence.append(f"status:{sig_signals.status}")

    return evidence


def _match_all(sig_signals: _Signals) -> list[tuple[Signature, list[str]]]:
    """Match every signature, preserving SIGNATURES order. Returns (sig, evidence)."""
    out: list[tuple[Signature, list[str]]] = []
    for sig in SIGNATURES:
        evidence = _match_signature(sig, sig_signals)
        if evidence:
            out.append((sig, evidence))
    return out


# ---------------------------------------------------------------------------
# Target -> signals adapters (URL str / httpx.Response / Playwright page)
# ---------------------------------------------------------------------------

# A small set of JS globals we read off a live Playwright page to catch tells
# that only exist after the vendor's script runs. Mapped to the body substring
# the corresponding signature already looks for, so a hit lands on the right
# vendor without growing the signature schema.
_PAGE_JS_GLOBAL_PROBE = r"""
() => {
  const present = [];
  const checks = {
    'grecaptcha': typeof window.grecaptcha !== 'undefined',
    'hcaptcha': typeof window.hcaptcha !== 'undefined',
    'window.rbzns': typeof window.rbzns !== 'undefined',
    'datadome': typeof window.DataDome !== 'undefined' || typeof window.dd !== 'undefined',
    'perimeterx': typeof window._pxAppId !== 'undefined' || typeof window.PX !== 'undefined',
    'kpsdk': typeof window.KPSDK !== 'undefined',
  };
  for (const [k, v] of Object.entries(checks)) { if (v) present.push(k); }
  return present;
}
"""


def _signals_from_httpx(resp: httpx.Response) -> _Signals:
    return _Signals(
        headers=dict(resp.headers),
        cookies=_cookies_from_headers(
            resp.headers,
            resp.headers.get_list("set-cookie")
            if hasattr(resp.headers, "get_list")
            else None,
        ),
        status=resp.status_code,
        body=_safe_text(resp) or "",
        url=str(resp.request.url) if resp.request is not None else None,
    ).finalize()


def _signals_from_url(url: str) -> _Signals:
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
        resp = client.get(url)
    sig = _signals_from_httpx(resp)
    sig.url = url
    return sig


def _is_playwright_page(obj: Any) -> bool:
    """A Playwright Page exposes both goto() and context."""
    return (
        hasattr(obj, "goto")
        and callable(getattr(obj, "goto"))
        and hasattr(obj, "context")
    )


def _signals_from_page(page: Any) -> _Signals:
    """Pull signals off a *live* Playwright page (cookies, body, JS globals).

    A page has no single response object, so we read what is observable now:
    the cookie jar, the rendered HTML (which carries script-src hosts and most
    in-page tells), the URL, and a handful of JS globals. Headers and status are
    not reliably available from a Page alone, so they stay empty.
    """
    cookies: dict[str, str] = {}
    body = ""
    url: str | None = None
    extra_body_tells: list[str] = []

    try:
        url = page.url
    except Exception:
        url = None

    try:
        jar = page.context.cookies()
        cookies = {c["name"]: c.get("value", "") for c in jar if "name" in c}
    except Exception:
        cookies = {}

    try:
        body = page.content() or ""
    except Exception:
        body = ""

    try:
        present = page.evaluate(_PAGE_JS_GLOBAL_PROBE)
        if isinstance(present, (list, tuple)):
            extra_body_tells = [str(x) for x in present]
    except Exception:
        extra_body_tells = []

    # Fold the JS-global hits into the body blob so signature body-matching
    # picks them up (each probe key is also a body substring some signature
    # already looks for).
    if extra_body_tells:
        body = body + "\n" + "\n".join(extra_body_tells)

    return _Signals(headers={}, cookies=cookies, status=None, body=body, url=url).finalize()


def _signals_from_target(target: Any) -> _Signals:
    """Duck-type ``target`` into normalised signals.

    ``target`` may be a URL ``str`` (an httpx GET is issued), an
    ``httpx.Response`` (analysed directly), a live Playwright ``Page`` (cookies
    + body + JS globals are read), or a Playwright ``Response`` (headers +
    status + body, plus the owning context's cookies if reachable).
    """
    if isinstance(target, str):
        return _signals_from_url(target)

    if isinstance(target, httpx.Response):
        return _signals_from_httpx(target)

    if _is_playwright_page(target):
        return _signals_from_page(target)

    # Otherwise: treat as a Playwright Response (duck-typed: .headers/.status).
    return _signals_from_playwright_response(target)


def _signals_from_playwright_response(resp: Any) -> _Signals:
    try:
        raw_headers = resp.headers  # dict in Playwright
    except Exception as exc:  # pragma: no cover - bad input
        raise TypeError(
            "identify_waap/fingerprint expect a URL str, httpx.Response, "
            "Playwright Page, or Playwright Response "
            f"(got {type(resp).__name__})"
        ) from exc

    headers = dict(raw_headers) if isinstance(raw_headers, Mapping) else dict(raw_headers)
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

    url = None
    try:
        url = resp.url
        if callable(url):
            url = url()
    except Exception:
        url = None

    return _Signals(
        headers=headers,
        cookies=cookies,
        status=status if isinstance(status, int) else None,
        body=_playwright_response_body(resp) or "",
        url=url if isinstance(url, str) else None,
    ).finalize()


# ---------------------------------------------------------------------------
# Public WAAP API
# ---------------------------------------------------------------------------

def identify_waap(url_or_response: Union[str, httpx.Response, Any]) -> list[str]:
    """Fingerprint the WAAP/anti-bot vendor(s) in front of a target.

    Accepts either:
      * a URL string — an httpx GET is issued (redirects followed, browser-ish
        UA) and its headers/cookies/status/body are analysed; or
      * an ``httpx.Response`` — analysed directly (no network call); or
      * a live Playwright ``Page`` — its cookies, rendered HTML and a few JS
        globals are analysed; or
      * a Playwright ``Response`` — its headers, status and body are analysed,
        plus the owning context's cookies if reachable.

    Returns a de-duplicated, stably-ordered (``SIGNATURES`` order) list of
    detected vendor names — e.g. ``Reblaze/Link11``, ``Akamai``, ``DataDome``,
    ``PerimeterX/HUMAN``, ``Kasada``, ``Imperva/Incapsula``, ``Cloudflare``,
    ``AWS WAF``, ``F5 BIG-IP/Shape``, ``reCAPTCHA``, ``hCaptcha``,
    ``SiteMinder``. Empty list means none recognised.

    This is the back-compatible thin wrapper over :func:`fingerprint`; use
    :func:`fingerprint` when you want tiers, strategies and evidence.
    """
    sig_signals = _signals_from_target(url_or_response)
    return [sig.name for sig, _ev in _match_all(sig_signals)]


def fingerprint(target: Union[str, httpx.Response, Any]) -> dict:
    """Structured WAAP fingerprint of a target.

    Same accepted target types as :func:`identify_waap` (URL str, httpx
    response, live Playwright page, or Playwright response).

    :returns: a dict of the shape::

        {
          "url": str | None,
          "status": int | None,
          "vendors": [
            {
              "name": str,                # canonical vendor name
              "tier": int,                # 1 engine / 2 smarter / 3 solver
              "strategy": str,            # short how-to-pass hint
              "evidence": [str, ...],     # which signals matched
              "clearance_cookies": [str], # cookies marking a cleared session
            },
            ...
          ],
        }

    ``vendors`` is in stable ``SIGNATURES`` order and empty if nothing matched.
    """
    sig_signals = _signals_from_target(target)
    vendors: list[dict] = []
    for sig, evidence in _match_all(sig_signals):
        vendors.append(
            {
                "name": sig.name,
                "tier": sig.tier,
                "strategy": sig.strategy,
                "evidence": evidence,
                "clearance_cookies": list(sig.clearance_cookies),
            }
        )
    return {
        "url": sig_signals.url,
        "status": sig_signals.status,
        "vendors": vendors,
    }


# ---------------------------------------------------------------------------
# Low-level helpers (cookie/body extraction)
# ---------------------------------------------------------------------------

def _cookies_from_headers(headers: Mapping[str, str], raw_set_cookie: Any = None) -> dict:
    """Extract cookie *names*+values from Set-Cookie headers."""
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
