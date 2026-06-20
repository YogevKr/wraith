"""reCAPTCHA-v3 reputation lifting — the GENERAL "high-score" capability.

What this module is (and is NOT)
--------------------------------
reCAPTCHA **v3** has no client-side puzzle and therefore no "solver". When the
page runs ``grecaptcha.execute(sitekey, {action})`` Google mints an opaque token
whose eventual **reputation score** (``0.0..1.0``, read by the site's backend via
``siteverify``) is decided *at mint time* from the identity that produced it:
its Google account cookies, aged history, device and IP reputation. A fresh,
automated profile scores like a bot (~0.1-0.3) no matter how good the stealth
engine; a real, warmed, logged-in browser scores ~0.9.

This module does not forge, replay, or "solve" anything. It **lifts the score**
of the context that mints the token by making a *warmed Google reputation* reach
the third-party reCAPTCHA iframe — the same reputation a real visitor's browser
carries for free. That is GENERAL: it is independent of the target sitekey,
action, or site, because the score is minted inside the ``www.google.com``
reCAPTCHA iframe from the ``.google.com`` cookies present in the context.

THE PROVEN RECIPE (validated live against El Al's ``/api/login`` oracle, 511->200)
---------------------------------------------------------------------------------
1. The v3 score is minted inside the ``www.google.com`` reCAPTCHA iframe, which
   reads the ``.google.com`` cookies present in the browser context. Injecting a
   real, logged-in Google identity's reputation cookies (SID / SAPISID /
   __Secure-3PSID / HSID / SSID / NID / _GRECAPTCHA ...) makes the token score
   high. This works across any sitekey/site — it is GENERAL.

2. THE RELIABILITY FIX — the reputation cookies must actually *reach* the
   third-party ``google.com`` iframe:

   * Inject each reputation cookie with ``secure=True`` and ``sameSite="None"``.
     A naive Firefox->Playwright sameSite mapping drops a non-secure ``None``
     cookie down to ``Lax``, so SAPISID/HSID/SSID would NOT be sent cross-site
     -> the token scores below threshold -> 511. ``identity.inject_cookies(...,
     third_party=True)`` forces this. (See :data:`identity.GOOGLE_REPUTATION_COOKIES`.)

   * Launch Camoufox with :data:`UNPARTITION_PREFS` so 3rd-party cookies are not
     partitioned/isolated away from the iframe::

         {"network.cookie.cookieBehavior": 0,
          "privacy.partition.network_state": False,
          "privacy.firstparty.isolate": False}

   With the full reputation set delivered AND un-partitioned, the token scores
   high and the protected endpoint accepts.

3. The v3 score is RUN-VARIABLE and there is NO trustworthy score readout for a
   third-party sitekey (a reCAPTCHA *demo*-key reading ~0.9 is cold-start noise,
   not your real score). VERIFY success against the **real protected endpoint**
   (accept vs reject), and optionally confirm the ``/recaptcha/api2/reload``
   request actually carried ``SID``/``SAPISID`` in its cookie header — that is
   the proof the reputation reached the iframe.

Honest limits
-------------
* You cannot replay or re-score an already-minted token — score is set at mint.
* ``www.recaptcha.net`` (and some Enterprise deployments) may deliberately read
  cookies from a host *other* than ``.google.com``; injected ``.google.com``
  reputation may then be ignored. :func:`ensure_high_score` warns on the
  ``recaptcha.net`` host. Verify on the real endpoint regardless.
* Borrowing the user's PRIMARY Google identity is anomalous-session / 2FA risk
  for that account; it is opt-in. Prefer the account-free Tier-0 floor-lift
  (:class:`PersistentGrecaptcha`) or a dedicated burner (:class:`WarmedAccount`).

Composition
-----------
This module is engine-agnostic and imports :mod:`wraith.identity` and
:mod:`wraith.detect` *lazily inside functions* to avoid import cycles. The engine
must be launched with :data:`UNPARTITION_PREFS` merged into ``firefox_user_prefs``
for the Camoufox engine (the agent/CLI wiring does this); for the Chromium engine
the prefs are simply ignored.
"""

from __future__ import annotations

import abc
import warnings
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "RecaptchaParams",
    "PlacementSpec",
    "UNPARTITION_PREFS",
    "ReputationSource",
    "BorrowedGoogleCookies",
    "PersistentGrecaptcha",
    "WarmedAccount",
    "ensure_high_score",
]


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class RecaptchaParams:
    """What we could discover about a page's reCAPTCHA-v3 deployment.

    This is the canonical home of :class:`RecaptchaParams`; :mod:`wraith.detect`
    imports it from here (``from wraith.recaptcha_v3 import RecaptchaParams``) for
    its :func:`detect.recaptcha_params` probe.

    Attributes:
        version: ``"v3"``, ``"v2"``, or ``"none"`` when no reCAPTCHA was found.
        enterprise: True if the page uses ``grecaptcha.enterprise`` /
            ``enterprise.js`` / ``recaptchaenterprise.googleapis.com``.
        sitekey: the discovered site key (``data-sitekey`` / ``?render=`` /
            iframe ``?k=``), or ``""`` if none was found.
        actions: best-effort list of v3 action labels observed on the page.
        host: the reCAPTCHA API host serving the iframe —
            ``"www.google.com"`` (reputation-friendly) or ``"www.recaptcha.net"``
            (may ignore ``.google.com`` cookies; a warning is emitted).
    """

    version: str = "none"
    enterprise: bool = False
    sitekey: str = ""
    actions: list[str] = field(default_factory=list)
    host: str = "www.google.com"


@dataclass
class PlacementSpec:
    """Where a reputation cookie (or artefact) is placed.

    A small descriptor used by reputation sources / wiring to record *where* a
    given artefact lives — e.g. ``where="context"`` for cookies injected into a
    Playwright BrowserContext, ``where="profile"`` for an on-disk persistent
    profile dir. ``name`` is the human-readable identifier (cookie name, profile
    dir, account label).
    """

    where: str
    name: str


# --------------------------------------------------------------------------- #
# The un-partition prefs (the engine-side half of the reliability fix)
# --------------------------------------------------------------------------- #
# Camoufox/Firefox partitions and isolates third-party cookies by default. The
# reCAPTCHA score is minted in a 3rd-party www.google.com iframe, so the injected
# .google.com reputation cookies must NOT be partitioned away from it. These
# prefs un-partition the 3p cookie state so SAPISID/HSID/SSID/etc. actually reach
# the iframe. Merge this into the Camoufox launch's `firefox_user_prefs`.
#   * network.cookie.cookieBehavior = 0  -> accept all cookies (incl. 3rd-party)
#   * privacy.partition.network_state = False -> don't partition network state by
#       top-level origin (dFPI off for cookies/network)
#   * privacy.firstparty.isolate = False -> don't isolate by first party
UNPARTITION_PREFS: dict[str, Any] = {
    "network.cookie.cookieBehavior": 0,
    "privacy.partition.network_state": False,
    "privacy.firstparty.isolate": False,
}


# --------------------------------------------------------------------------- #
# Reputation sources
# --------------------------------------------------------------------------- #

class ReputationSource(abc.ABC):
    """A source of Google reputation to lift the minted v3 score.

    A reputation source knows how to make a warmed Google reputation present in a
    Playwright/Camoufox :class:`BrowserContext` *before* the page mints a
    reCAPTCHA-v3 token. Concrete sources differ in WHERE the reputation comes
    from and how much account risk they carry:

    * :class:`PersistentGrecaptcha` — Tier-0, account-free. The persistent
      profile ages its own ``_GRECAPTCHA`` cookie over time; the lowest-risk
      floor-lift. Default.
    * :class:`WarmedAccount` — a dedicated *burner* Google account warmed in its
      own profile dir. Medium risk, isolated from the user's real identity.
    * :class:`BorrowedGoogleCookies` — borrows the user's PRIMARY Google
      identity's reputation cookies off disk. Highest score, highest risk
      (anomalous-session / 2FA on the real account); strictly opt-in.

    Subclasses implement :meth:`prepare`, which mutates ``context`` and returns
    the number of reputation cookies injected (0 for no-op sources that rely on a
    persistent profile dir instead).
    """

    @abc.abstractmethod
    def prepare(self, context: Any) -> int:
        """Make this source's reputation present in ``context``.

        :param context: a live Playwright/Camoufox ``BrowserContext`` (duck-typed
            — anything with ``add_cookies``). Sources that work via a persistent
            profile dir instead of injection may treat this as a no-op.
        :returns: the number of reputation cookies injected into ``context``
            (``0`` for profile-backed / no-op sources).
        """
        raise NotImplementedError


class BorrowedGoogleCookies(ReputationSource):
    """Borrow the user's *primary* Google reputation cookies off disk.

    .. warning::

        This borrows the reputation of a REAL, logged-in Google account by
        reading its ``.google.com`` cookies straight out of a local Firefox/Zen
        profile and injecting them third-party into the automation context. That
        account will see an anomalous session (new device/IP) and may trip a 2FA
        / security challenge or re-auth. **Opt-in only**, and never against an
        account you cannot afford to have flagged. For a low-risk floor-lift use
        :class:`PersistentGrecaptcha`; for an isolated medium-risk lift use a
        dedicated burner via :class:`WarmedAccount`.

    This is the strongest source — a fully warmed human identity — and is the
    one that drove El Al's ``/api/login`` oracle from 511 to 200.

    :param profile_substring: optional case-insensitive substring to pick a
        specific profile path when several Firefox/Zen profiles exist (e.g.
        ``"default-release"`` or a profile folder name). If ``None``, the first
        discovered profile that yields reputation cookies is used.
    """

    def __init__(self, profile_substring: str | None = None) -> None:
        self.profile_substring = profile_substring
        #: the profile path actually used by the last successful :meth:`prepare`.
        self.used_profile: str | None = None

    def _candidate_profiles(self) -> list[Any]:
        # Lazy import to avoid an import cycle (identity -> ... -> recaptcha_v3).
        from wraith import identity

        profiles = list(identity.find_zen_profiles()) + list(
            identity.find_firefox_profiles()
        )
        if self.profile_substring:
            needle = self.profile_substring.lower()
            profiles = [p for p in profiles if needle in str(p).lower()]
        return profiles

    def prepare(self, context: Any) -> int:
        """Extract the borrowed Google identity and inject it third-party.

        Walks the discovered Zen/Firefox profiles, takes the first that carries a
        logged-in Google session (any :data:`identity.GOOGLE_REPUTATION_COOKIES`),
        and injects that profile's **entire** ``google.com`` cookie jar. The
        reputation cookies get the proven third-party delivery fix (``secure=True``
        + ``sameSite="None"`` via :func:`identity.inject_cookies(...,
        third_party=True)`); the remaining google cookies are injected with their
        natural attributes. Live validation showed the full jar is required —
        the named subset alone scores below threshold (511 vs 200).

        :returns: the number of cookies injected (``0`` if no profile carried a
            logged-in Google session — or a Chrome-only profile whose cookies are
            encrypted).
        """
        from wraith import identity

        profiles = self._candidate_profiles()
        if not profiles:
            warnings.warn(
                "BorrowedGoogleCookies: no Firefox/Zen profiles found to borrow a "
                "Google identity from. The minted reCAPTCHA-v3 score will not be "
                "lifted. (Chrome profiles are encrypted and not usable here — see "
                "wraith.identity.)",
                RuntimeWarning,
                stacklevel=2,
            )
            return 0

        last_err: Exception | None = None
        for prof in profiles:
            try:
                # Inject the FULL google.com jar, not just the named reputation
                # subset. Live validation against El Al's /api/login oracle proved
                # the subset (~40 cookies) scores 511 while the full jar (~87)
                # scores 200: the non-reputation google cookies (AEC,
                # __Secure-ENID, OTZ, consent ...) also feed the v3 score. We
                # split the jar so only the reputation names get the third-party
                # secure+SameSite=None forcing (the proven delivery fix); the rest
                # keep their natural attributes — exactly the recipe that scored 200.
                cookies = identity.extract_cookies(prof, domain_filter="google.com")
            except Exception as exc:  # unreadable / encrypted / locked profile
                last_err = exc
                continue
            if not cookies:
                continue
            rep = [c for c in cookies if c.name in identity.GOOGLE_REPUTATION_COOKIES]
            if not rep:
                # A google.com jar with no reputation cookies = not logged in.
                continue
            rest = [c for c in cookies if c.name not in identity.GOOGLE_REPUTATION_COOKIES]
            injected = identity.inject_cookies(context, rep, third_party=True)
            injected += identity.inject_cookies(context, rest, third_party=False)
            if injected:
                self.used_profile = str(prof)
                return injected

        warnings.warn(
            "BorrowedGoogleCookies: found profile(s) but none yielded Google "
            "reputation cookies (is a Google account logged in in that browser?). "
            f"The score will not be lifted. Last error: {last_err!r}",
            RuntimeWarning,
            stacklevel=2,
        )
        return 0


class PersistentGrecaptcha(ReputationSource):
    """Tier-0, account-free reputation floor-lift via a persistent profile.

    This is the DEFAULT low-risk source and the recommended baseline. It uses NO
    Google account: instead it relies on the browser running from a *persistent*
    user-data dir, which over time ages its own ``_GRECAPTCHA`` cookie (and other
    benign reCAPTCHA state). A profile that has organically loaded reCAPTCHA a few
    times carries a small reputation that lifts the v3 score off the cold-start
    floor — without ever logging in, and so without account risk.

    There is nothing to *inject*: the reputation lives in the on-disk profile, so
    :meth:`prepare` is a documented no-op. The engine MUST be launched with
    ``profile_dir`` pointing at this same persistent directory, otherwise there is
    no aged ``_GRECAPTCHA`` to carry the lift.

    :param profile_dir: the persistent user-data dir the engine is (or must be)
        launched with. Recorded for diagnostics / wiring.
    """

    def __init__(self, profile_dir: str) -> None:
        if not profile_dir:
            raise ValueError("profile_dir is required for PersistentGrecaptcha")
        self.profile_dir = profile_dir
        #: descriptor of where this source's reputation lives.
        self.placement = PlacementSpec(where="profile", name=str(profile_dir))

    def prepare(self, context: Any) -> int:
        """No-op: the reputation is the persistent profile, not injected cookies.

        Returns ``0`` always. For this source to actually lift the score the
        engine must have been launched with ``profile_dir=self.profile_dir`` so
        the aged ``_GRECAPTCHA`` cookie is present from the start of the session.
        """
        # Intentionally no injection. We do not even read `context`; the lift is
        # entirely a property of the persistent on-disk profile the engine runs.
        return 0


class WarmedAccount(ReputationSource):
    """Medium-risk reputation lift from a *dedicated burner* Google account.

    Like :class:`PersistentGrecaptcha` this is profile-backed (the burner account
    stays logged in inside its own persistent ``profile_dir``), so the engine must
    be launched with that same ``profile_dir`` and :meth:`prepare` is a no-op
    beyond recording the placement. Unlike :class:`PersistentGrecaptcha` it DOES
    carry a logged-in Google session — a throwaway account warmed for this
    purpose — which lifts the score further than the account-free Tier-0 floor,
    while keeping the user's primary identity out of harm's way (the opposite
    end of the risk spectrum from :class:`BorrowedGoogleCookies`).

    This is a skeleton: warming and maintaining the burner (logging it in,
    aging it, rotating it) is an operational concern outside this module. What it
    guarantees here is the same wiring contract as :class:`PersistentGrecaptcha`.

    :param profile_dir: the persistent user-data dir holding the warmed burner
        session; the engine must be launched with this dir.
    """

    def __init__(self, profile_dir: str) -> None:
        if not profile_dir:
            raise ValueError("profile_dir is required for WarmedAccount")
        self.profile_dir = profile_dir
        self.placement = PlacementSpec(where="profile", name=str(profile_dir))

    def prepare(self, context: Any) -> int:
        """No-op: the burner's reputation lives in its persistent profile.

        Returns ``0`` always. The engine must be launched with
        ``profile_dir=self.profile_dir`` so the burner's logged-in Google session
        is present for the iframe to read.
        """
        return 0


# --------------------------------------------------------------------------- #
# The reload-cookie verifier (the proof the reputation reached the iframe)
# --------------------------------------------------------------------------- #

# Reputation cookie names that, if present in the /recaptcha/api2/reload request's
# Cookie header, prove the .google.com reputation actually reached the 3rd-party
# iframe. SAPISID/SID are the load-bearing pair.
_RELOAD_PROOF_COOKIES = ("SAPISID", "SID", "__Secure-3PSID", "__Secure-1PSID")


def _host_of(url: str) -> str:
    """Best-effort hostname from a URL string (no exceptions)."""
    try:
        from urllib.parse import urlparse

        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _cookie_header_has_reputation(cookie_header: str) -> bool:
    """True if a Cookie header string carries any reload-proof reputation cookie."""
    if not cookie_header:
        return False
    # Cookie header is "name=value; name2=value2"; check names defensively.
    present = set()
    for part in cookie_header.split(";"):
        name = part.split("=", 1)[0].strip()
        if name:
            present.add(name)
    return any(name in present for name in _RELOAD_PROOF_COOKIES)


class _ReloadCookieWitness:
    """One-shot listener that records whether a /recaptcha/api2/reload request
    carried reputation cookies. Attaches to a Playwright page's ``request`` event.

    Playwright does not expose request headers synchronously for every transport,
    so we read ``request.headers`` (lower-cased keys) and look for a ``cookie``
    header. Some engines strip the Cookie header from the JS-visible
    ``request.headers`` for security; in that case ``saw_reload`` may be True
    while ``carried_reputation`` stays False/None — which is itself a soft signal
    rather than proof of failure.
    """

    def __init__(self) -> None:
        self.saw_reload: bool = False
        self.carried_reputation: bool | None = None

    def __call__(self, request: Any) -> None:
        try:
            url = request.url
        except Exception:
            return
        if "/recaptcha/api2/reload" not in url and "/recaptcha/enterprise/reload" not in url:
            return
        self.saw_reload = True
        # Try the simple sync headers first; fall back to all_headers() if the
        # engine exposes it (async on some versions — guard with try/except).
        cookie_header = ""
        try:
            headers = request.headers or {}
            cookie_header = headers.get("cookie", "") or headers.get("Cookie", "")
        except Exception:
            cookie_header = ""
        if cookie_header:
            self.carried_reputation = _cookie_header_has_reputation(cookie_header)


# --------------------------------------------------------------------------- #
# The public entry point
# --------------------------------------------------------------------------- #

# Cache of contexts/hosts we've already prepared so ensure_high_score is
# idempotent per (context identity, reCAPTCHA host). Keyed by (id(context), host)
# -> RecaptchaParams. Using id() avoids holding a strong ref to the context.
_PREPARED: dict[tuple[int, str], RecaptchaParams] = {}


def ensure_high_score(
    page: Any,
    *,
    source: ReputationSource | None = None,
    self_check: bool = False,
    verify_reload_cookies: bool = True,
) -> RecaptchaParams:
    """Lift the reCAPTCHA-v3 score this ``page`` will mint, the GENERAL way.

    Pipeline (encodes the PROVEN RECIPE — see the module docstring):

    1. Probe the page with :func:`wraith.detect.recaptcha_params` to learn the
       version / sitekey / host / enterprise flag. If the host is
       ``www.recaptcha.net`` a warning is emitted — that host may ignore the
       injected ``.google.com`` reputation, so verify on the real endpoint.
    2. If ``source`` is given, call ``source.prepare(page.context)`` to make a
       warmed Google reputation present in the context (cookies injected
       third-party with ``secure=True`` + ``sameSite="None"`` for the
       cookie-injecting sources; a no-op for the persistent-profile sources).
       The engine must already have been launched with
       :data:`UNPARTITION_PREFS` merged into its ``firefox_user_prefs`` for the
       cookies to reach the 3rd-party iframe — this function does NOT relaunch
       the engine (it can only set cookies/prefs on an already-running context if
       the caller wired the prefs at launch).
    3. If ``verify_reload_cookies`` is True, install a one-shot listener and
       confirm a ``/recaptcha/api2/reload`` request carried ``SID``/``SAPISID``
       in its cookie header. If a reload is seen WITHOUT reputation cookies a
       warning is emitted — that is the classic "cookies aren't reaching the
       iframe" symptom (wrong sameSite, or partitioned 3p state).

    This is idempotent and cached per ``(context, reCAPTCHA host)``: calling it
    again for the same context+host returns the cached :class:`RecaptchaParams`
    without re-injecting.

    Honest limits (do not skip):
        * The score is minted, not solved — you cannot replay or re-score an
          existing token; mint fresh after preparing.
        * ``www.recaptcha.net`` / some Enterprise deployments may ignore
          ``.google.com`` reputation; the lift can be a no-op there.
        * The v3 score is RUN-VARIABLE and there is no trustworthy client-side
          readout for a 3rd-party sitekey. **Always verify success against the
          real protected endpoint (accept vs reject).** ``self_check`` only
          reads the cold-start demo score and is diagnostic noise, not proof.

    :param page: a live Playwright/Camoufox ``Page`` on (or about to be on) the
        target site.
    :param source: the reputation source to prepare into ``page.context``; if
        ``None``, no reputation is injected (probe + verify only).
    :param self_check: if True, additionally read the run-variable demo-key score
        via :func:`wraith.detect.recaptcha_v3_score` for diagnostics. Off by
        default because it is misleading for a 3rd-party sitekey.
    :param verify_reload_cookies: install the one-shot reload-cookie witness and
        warn if a reload is seen that did not carry reputation cookies.
    :returns: the :class:`RecaptchaParams` discovered for the page (``version``
        ``"none"`` if the page has no reCAPTCHA).
    """
    # Lazy imports — keep import wraith.recaptcha_v3 cheap and cycle-free.
    from wraith import detect

    params = detect.recaptcha_params(page)

    # Establish a per-(context, host) cache key. id(context) is stable for the
    # life of the context object; host distinguishes google.com vs recaptcha.net.
    context = getattr(page, "context", None)
    cache_key = (id(context) if context is not None else id(page), params.host or "")

    if cache_key in _PREPARED:
        return _PREPARED[cache_key]

    if params.version == "none":
        warnings.warn(
            "ensure_high_score: no reCAPTCHA detected on this page yet. If the "
            "widget loads lazily, navigate/interact first, then call again. "
            "Nothing to lift right now.",
            RuntimeWarning,
            stacklevel=2,
        )

    if params.host == "www.recaptcha.net":
        warnings.warn(
            "ensure_high_score: this page serves reCAPTCHA from www.recaptcha.net, "
            "which may read reputation from a host other than .google.com — your "
            "injected .google.com reputation could be ignored. Verify on the real "
            "protected endpoint (accept vs reject).",
            RuntimeWarning,
            stacklevel=2,
        )

    # 2. Inject / activate the reputation.
    if source is not None and context is not None:
        try:
            injected = source.prepare(context)
        except Exception as exc:
            warnings.warn(
                f"ensure_high_score: reputation source {type(source).__name__} "
                f"failed to prepare: {exc!r}. The score will not be lifted.",
                RuntimeWarning,
                stacklevel=2,
            )
            injected = 0
        if injected == 0 and isinstance(source, BorrowedGoogleCookies):
            # BorrowedGoogleCookies already warns internally; nothing to add.
            pass

    # 3. Install the one-shot reload-cookie witness for verification. We attach
    # it and leave it attached for the life of the page; it only fires on the
    # reCAPTCHA reload request and is cheap. We surface a warning lazily the next
    # time it is consulted is not possible (no future hook), so we attach and the
    # caller / CLI can read params + the warning is emitted on the request event.
    if verify_reload_cookies:
        witness = _ReloadCookieWitness()

        def _on_request(request: Any, _w: _ReloadCookieWitness = witness) -> None:
            _w(request)
            if _w.saw_reload and _w.carried_reputation is False:
                warnings.warn(
                    "ensure_high_score: a /recaptcha/api2/reload request did NOT "
                    "carry SID/SAPISID — the borrowed .google.com reputation is "
                    "not reaching the 3rd-party iframe. Check that cookies were "
                    "injected third_party=True (secure + sameSite=None) AND that "
                    "the engine launched with UNPARTITION_PREFS in "
                    "firefox_user_prefs. Score will stay at the bot floor.",
                    RuntimeWarning,
                    stacklevel=2,
                )

        try:
            page.on("request", _on_request)
        except Exception:
            # Page does not support event subscription (e.g. a stub); skip.
            pass

    # Optional, explicitly-diagnostic-only demo-key readout.
    if self_check:
        try:
            demo = detect.recaptcha_v3_score(page)
            warnings.warn(
                f"ensure_high_score: demo-key reCAPTCHA-v3 score read {demo:.2f} "
                "— this is COLD-START NOISE for a 3rd-party sitekey, not your real "
                "score. Verify on the real protected endpoint.",
                RuntimeWarning,
                stacklevel=2,
            )
        except Exception as exc:
            warnings.warn(
                f"ensure_high_score: self_check score read failed: {exc!r}",
                RuntimeWarning,
                stacklevel=2,
            )

    _PREPARED[cache_key] = params
    return params
