"""reCAPTCHA helpers for Wraith — mint tokens, read scores, and (optionally)
delegate to third-party solver farms.

Conceptual model (read this before reaching for a solver service)
-----------------------------------------------------------------
reCAPTCHA **v3** is *scoreless from the client's point of view*: there is no
puzzle and no client-side "solver". When ``grecaptcha.execute(sitekey, {action})``
runs in the page, Google returns an opaque token. The site's backend then calls
``siteverify`` and receives a reputation **score** in ``0.0..1.0``. That score is
decided **at mint time** from the reputation of the browsing identity that
produced the token — cookies/SID, IP reputation, device + behavioural history.

Consequences that drive this module's design:

* **The score is set at MINT, not solved.** You cannot "raise" a token's score
  after the fact. To get a high score you must mint from a *warmed / borrowed*
  identity (a real, aged, logged-in profile — see :mod:`wraith.identity`).
* **Tokens are single-use, short-lived (~120 s), and action-bound.** A token
  minted for ``action="login"`` will fail server-side verification if the
  backend expected ``action="checkout"``. Reuse within the ~120 s window is only
  meaningful if the backend has not yet consumed it.
* **Solver services are cold farms.** CapSolver / 2Captcha mint from their own
  throwaway residential identities, so v3 tokens they return typically score
  around ~0.1 — usually *below* a site's ``min_score`` threshold. They are a last
  resort and are far weaker than minting from a warmed local session.
  They are genuinely useful for v2 (image) challenges, which Wraith does not try
  to forge here.

reCAPTCHA **v2** ("I'm not a robot" / image grids) *does* have a real token a
human or farm produces by completing the challenge; that is what the solver
services excel at and what :class:`SolverService` is shaped for.

ToS boundary
------------
Automating or outsourcing CAPTCHA solving generally violates Google's Terms of
Service and very often the target site's ToS as well. The
:class:`SolverService` integrations here are **bring-your-own-key** scaffolding
provided for interoperability and research; you are responsible for using them
lawfully and only against systems you are authorised to test. Prefer
:func:`harvest_token` from a warmed/borrowed session, which uses the page's own
``grecaptcha`` exactly as a real visitor's browser would.

Public API
----------
* :func:`harvest_token` — execute the page's own ``grecaptcha`` and return a
  fresh token, minted with the current (ideally warmed) identity.
* :func:`score` — read the v3 reputation score for the current identity
  (delegates to :func:`wraith.detect.recaptcha_v3_score`).
* :class:`SolverService` — ABC for external solver farms, with
  :class:`CapSolver` and :class:`TwoCaptcha` skeletons.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "harvest_token",
    "inject_token",
    "score",
    "Challenge",
    "SolverService",
    "CapSolver",
    "TwoCaptcha",
]


# ---------------------------------------------------------------------------
# Token harvesting (the preferred path)
# ---------------------------------------------------------------------------

# JS run inside the page. We resolve grecaptcha (enterprise or classic), wait
# for .ready(), then call .execute(sitekey, {action}) and return the token.
# Written as an async IIFE returning a Promise so Playwright's page.evaluate
# awaits it; errors are surfaced as a rejected promise -> Python exception.
_HARVEST_JS = """
async ([sitekey, action, enterprise, timeoutMs]) => {
    const root = enterprise
        ? (window.grecaptcha && window.grecaptcha.enterprise)
        : window.grecaptcha;
    if (!root || typeof root.execute !== 'function') {
        throw new Error(
            'grecaptcha' + (enterprise ? '.enterprise' : '') +
            ' is not present on this page (is the reCAPTCHA script loaded?)'
        );
    }

    // grecaptcha.ready may be on the enterprise object or the root; fall back
    // to a no-op resolve if neither exposes it.
    const readyFn = (typeof root.ready === 'function')
        ? root.ready.bind(root)
        : ((window.grecaptcha && typeof window.grecaptcha.ready === 'function')
            ? window.grecaptcha.ready.bind(window.grecaptcha)
            : null);

    await new Promise((resolve) => {
        if (readyFn) { readyFn(resolve); } else { resolve(); }
    });

    const exec = root.execute(sitekey, { action });
    // execute() returns a Promise<string>; race it against a timeout.
    const token = await Promise.race([
        Promise.resolve(exec),
        new Promise((_, reject) =>
            setTimeout(() => reject(new Error('grecaptcha.execute timed out')),
                       timeoutMs)),
    ]);

    if (!token || typeof token !== 'string') {
        throw new Error('grecaptcha.execute returned an empty token');
    }
    return token;
}
"""


def harvest_token(
    page: Any,
    sitekey: str,
    action: str = "submit",
    *,
    timeout: float = 30.0,
    enterprise: bool = False,
) -> str:
    """Mint a fresh reCAPTCHA-v3 token from the page's own ``grecaptcha``.

    This runs the *site's* reCAPTCHA client the same way a real visitor's
    browser does: it waits for ``grecaptcha[.enterprise].ready`` then calls
    ``execute(sitekey, {action})`` and returns the opaque token string.

    The token's eventual reputation **score is determined at this mint** from
    the identity that ``page`` is running as. For a usable score, mint from a
    *warmed* or *borrowed* session (see :mod:`wraith.identity`) rather than a
    fresh headless context.

    The returned token is:

    * **single-use** — once the target backend verifies it, it is spent;
    * **short-lived** — Google expires v3 tokens after roughly **120 s**, so
      mint immediately before you submit and do not cache across requests;
    * **action-bound** — it carries ``action`` and the backend may reject it if
      the action does not match what it expects.

    :param page: a live Playwright ``Page`` already on the site that hosts the
        reCAPTCHA (the ``grecaptcha`` global must be loaded in this page).
    :param sitekey: the site key (``data-sitekey`` / the key passed to
        ``grecaptcha.render``/``execute``).
    :param action: the v3 action label to bind the token to (default
        ``"submit"``). Match what the site uses for the flow you are driving.
    :param timeout: seconds to wait for ``grecaptcha.execute`` to resolve.
    :param enterprise: use ``grecaptcha.enterprise`` instead of the classic
        ``grecaptcha`` namespace (reCAPTCHA Enterprise sites).
    :returns: the freshly minted token string.
    :raises RuntimeError: if ``grecaptcha`` is unavailable or ``execute`` fails
        / times out.
    """
    try:
        token = page.evaluate(
            _HARVEST_JS,
            [sitekey, action, bool(enterprise), int(timeout * 1000)],
        )
    except Exception as exc:  # Playwright re-raises the JS Error as Error
        raise RuntimeError(
            f"failed to harvest reCAPTCHA token for sitekey={sitekey!r} "
            f"action={action!r} (enterprise={enterprise}): {exc}"
        ) from exc

    if not isinstance(token, str) or not token:
        raise RuntimeError(
            "grecaptcha.execute returned no token "
            f"(sitekey={sitekey!r}, action={action!r})"
        )
    return token


# ---------------------------------------------------------------------------
# Token injection (consume a solver/farm token)
# ---------------------------------------------------------------------------

# Default hidden response fields by widget. A solver returns a token; the page
# only consumes it once it lands in the right field and an input/change fires.
_RESPONSE_FIELDS = (
    "g-recaptcha-response",   # reCAPTCHA v2/v3
    "h-captcha-response",     # hCaptcha
    "cf-turnstile-response",  # Cloudflare Turnstile
    "fc-token",               # FunCaptcha / Arkose
)

_INJECT_JS = """
(args) => {
  const token = args.token;
  const names = args.field ? [args.field] : args.fields;
  const hit = [];
  for (const name of names) {
    const els = Array.from(document.getElementsByName(name));
    const byId = document.getElementById(name);
    if (byId && !els.includes(byId)) els.push(byId);
    for (const el of els) {
      try {
        el.value = token;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        hit.push(name);
      } catch (e) { /* ignore a single bad field */ }
    }
  }
  return hit;
}
"""


def inject_token(page: Any, token: str, *, field: str | None = None) -> list[str]:
    """Write a solved CAPTCHA token into the page's hidden response field(s).

    Makes a token obtained from a farm/solver (:class:`CapSolver`,
    :class:`TwoCaptcha`, …) actually *usable*: it sets the matching hidden
    response field — ``cf-turnstile-response`` / ``h-captcha-response`` /
    ``g-recaptcha-response`` / ``fc-token`` (whichever exist on the page, or an
    explicit ``field``) — and fires ``input``/``change`` so the page picks it up
    on submit. The sibling of :func:`harvest_token` (which mints a token from the
    page's own widget); use this when the token came from off-box.

    :returns: the list of field names actually written (empty if none matched).
    """
    return list(
        page.evaluate(_INJECT_JS, {"token": token, "field": field, "fields": list(_RESPONSE_FIELDS)})
        or []
    )


# ---------------------------------------------------------------------------
# Score reading (delegates to wraith.detect)
# ---------------------------------------------------------------------------


def score(target: Any) -> float:
    """Read the reCAPTCHA-v3 reputation score for the current identity.

    Thin delegate to :func:`wraith.detect.recaptcha_v3_score`. Drives the
    supplied browser to a public v3 tester and returns the score in
    ``0.0..1.0``. Low (~0.1-0.3) means the identity looks like a bot — borrow a
    warmed profile before minting real tokens; high (~0.9) means warmed.

    :param target: a live Playwright ``Page`` or a zero-arg callable returning
        one (whatever :func:`wraith.detect.recaptcha_v3_score` accepts).
    :returns: the score as a float in ``[0.0, 1.0]``.
    """
    # Imported lazily so `import wraith.recaptcha` stays cheap and never pulls
    # in detect's heavier dependency graph until a score is actually requested.
    from wraith.detect import recaptcha_v3_score

    return recaptcha_v3_score(target)


# ---------------------------------------------------------------------------
# External solver farms (bring-your-own-key; last resort — read module docstring)
# ---------------------------------------------------------------------------

# Map a detect.py WAAP/CAPTCHA vendor string to a solver challenge kind.
_VENDOR_TO_KIND: dict[str, str] = {
    "recaptcha": "recaptcha_v3",
    "recaptcha_v3": "recaptcha_v3",
    "recaptcha_v2": "recaptcha_v2",
    "hcaptcha": "hcaptcha",
    "cloudflare": "turnstile",
    "turnstile": "turnstile",
    "datadome": "datadome",
    "aws-waf": "awswaf",
    "awswaf": "awswaf",
    "funcaptcha": "funcaptcha",
    "arkose": "funcaptcha",
    "geetest": "geetest",
}


@dataclass
class Challenge:
    """A vendor-agnostic descriptor of a CAPTCHA/anti-bot challenge to solve.

    Lets :func:`wraith.detect.identify_waap` / :func:`recaptcha_params` feed a
    solver in one dispatch: build a ``Challenge`` from what was detected, then
    call :meth:`SolverService.solve_challenge`, which maps ``kind`` to the right
    farm task type (Turnstile / hCaptcha / reCAPTCHA v2+v3 / FunCaptcha / AWS
    WAF / …) instead of the reCAPTCHA-only :meth:`SolverService.solve`.
    """

    kind: str
    sitekey: str
    url: str
    action: str = "submit"
    data: Optional[str] = None          # Turnstile cData
    enterprise: bool = False
    min_score: float = 0.5

    @classmethod
    def from_vendor(cls, vendor: str, sitekey: str, url: str, **kw: Any) -> "Challenge":
        """Build a Challenge from a detected vendor string (see ``_VENDOR_TO_KIND``)."""
        kind = _VENDOR_TO_KIND.get(str(vendor).strip().lower(), "recaptcha_v3")
        return cls(kind=kind, sitekey=sitekey, url=url, **kw)


class SolverService(abc.ABC):
    """Abstract base for third-party CAPTCHA solver farms.

    Concrete subclasses wrap a vendor's create-task / poll-result HTTP API and
    return a usable reCAPTCHA token. **Read the module docstring first**: for
    reCAPTCHA *v3*, farm-minted tokens come from cold identities and typically
    score ~0.1, i.e. below most sites' thresholds — prefer
    :func:`harvest_token` from a warmed/borrowed session. These services are
    most useful for v2 image challenges.

    All implementations are bring-your-own-key and subject to the ToS caveats in
    the module docstring.
    """

    def __init__(self, api_key: str, *, timeout: float = 120.0) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.timeout = timeout

    @abc.abstractmethod
    def solve(
        self,
        sitekey: str,
        url: str,
        action: str = "submit",
        *,
        min_score: float = 0.5,
    ) -> str:
        """Obtain a reCAPTCHA token for ``sitekey`` on ``url``.

        :param sitekey: the target site's reCAPTCHA site key.
        :param url: the page URL where the token will be submitted.
        :param action: v3 action label to request (ignored for v2).
        :param min_score: minimum acceptable v3 score; implementations should
            request at least this and raise if the farm cannot meet it.
        :returns: the solved token string.
        :raises RuntimeError: on API error, timeout, or unmet ``min_score``.
        """
        raise NotImplementedError

    def _task_for(self, challenge: "Challenge") -> "tuple[dict, str]":
        """Return ``(createTask task dict, solution-field name)`` for a challenge.

        Implemented per provider (farm task-type names differ). Raise
        ``ValueError`` for an unsupported :class:`Challenge` kind.
        """
        raise NotImplementedError

    def solve_challenge(self, challenge: "Challenge") -> str:
        """Solve ANY supported challenge kind via this farm's createTask/poll API.

        Generalizes :meth:`solve` (reCAPTCHA-only) across Turnstile / hCaptcha /
        reCAPTCHA v2+v3 / FunCaptcha / AWS WAF by dispatching through
        :meth:`_task_for`. Pair the returned token with
        :func:`wraith.recaptcha.inject_token` to feed it back into the page.
        """
        import httpx

        task, field = self._task_for(challenge)
        payload = {"clientKey": self.api_key, "task": task}
        with httpx.Client(timeout=self.timeout) as client:
            created = client.post(f"{self.BASE_URL}/createTask", json=payload)
            created.raise_for_status()
            created = created.json()
            if created.get("errorId"):
                raise RuntimeError(
                    f"{type(self).__name__} createTask failed: "
                    f"{created.get('errorDescription') or created}"
                )
            task_id = created.get("taskId")
            if not task_id:
                raise RuntimeError(f"{type(self).__name__} returned no taskId: {created}")
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                r = client.post(
                    f"{self.BASE_URL}/getTaskResult",
                    json={"clientKey": self.api_key, "taskId": task_id},
                )
                r.raise_for_status()
                data = r.json()
                if data.get("errorId"):
                    raise RuntimeError(
                        f"{type(self).__name__} getTaskResult failed: "
                        f"{data.get('errorDescription') or data}"
                    )
                if data.get("status") == "ready":
                    sol = data.get("solution") or {}
                    token = sol.get(field) or sol.get("gRecaptchaResponse") or sol.get("token")
                    if not token:
                        raise RuntimeError(f"{type(self).__name__} ready but no token: {data}")
                    return token
                time.sleep(2.0)
        raise RuntimeError(f"{type(self).__name__} timed out waiting for task result")


class CapSolver(SolverService):
    """CapSolver (``api.capsolver.com``) integration skeleton.

    Bring your own ``api_key``. Uses CapSolver's ``/createTask`` +
    ``/getTaskResult`` endpoints. The request shape is real; the result-polling
    loop is left as a documented TODO so this never silently pretends to solve.

    See: https://docs.capsolver.com/
    """

    BASE_URL = "https://api.capsolver.com"

    # challenge kind -> (CapSolver task type, solution field holding the token)
    _TASK_TYPES = {
        "recaptcha_v3": ("ReCaptchaV3TaskProxyLess", "gRecaptchaResponse"),
        "recaptcha_v2": ("ReCaptchaV2TaskProxyLess", "gRecaptchaResponse"),
        "turnstile": ("AntiTurnstileTaskProxyLess", "token"),
        "hcaptcha": ("HCaptchaTaskProxyLess", "gRecaptchaResponse"),
        "funcaptcha": ("FunCaptchaTaskProxyLess", "token"),
        "awswaf": ("AntiAwsWafTaskProxyLess", "cookie"),
    }

    def _task_for(self, challenge: "Challenge") -> "tuple[dict, str]":
        if challenge.kind not in self._TASK_TYPES:
            raise ValueError(f"CapSolver: unsupported challenge kind {challenge.kind!r}")
        ttype, field = self._TASK_TYPES[challenge.kind]
        task: dict = {"type": ttype, "websiteURL": challenge.url, "websiteKey": challenge.sitekey}
        if challenge.kind == "recaptcha_v3":
            task["pageAction"] = challenge.action
            task["minScore"] = challenge.min_score
        if challenge.kind == "turnstile":
            meta = {k: v for k, v in (("action", challenge.action), ("cdata", challenge.data)) if v}
            if meta:
                task["metadata"] = meta
        if challenge.enterprise and challenge.kind in ("recaptcha_v3", "recaptcha_v2"):
            task["isEnterprise"] = True
        return task, field

    def solve(
        self,
        sitekey: str,
        url: str,
        action: str = "submit",
        *,
        min_score: float = 0.5,
    ) -> str:
        import httpx

        # ReCaptchaV3TaskProxyLess for v3; switch type to
        # ReCaptchaV2TaskProxyLess (drop pageAction/minScore) for v2.
        task = {
            "type": "ReCaptchaV3TaskProxyLess",
            "websiteURL": url,
            "websiteKey": sitekey,
            "pageAction": action,
            "minScore": min_score,
        }
        payload = {"clientKey": self.api_key, "task": task}

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.BASE_URL}/createTask", json=payload)
            resp.raise_for_status()
            created = resp.json()
            if created.get("errorId"):
                raise RuntimeError(
                    f"CapSolver createTask failed: "
                    f"{created.get('errorDescription') or created}"
                )
            task_id = created.get("taskId")
            if not task_id:
                raise RuntimeError(f"CapSolver returned no taskId: {created}")

            # TODO(byok): poll /getTaskResult until status == 'ready', honouring
            # self.timeout, then return result['solution']['gRecaptchaResponse'].
            # Sketch of the intended loop:
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                r = client.post(
                    f"{self.BASE_URL}/getTaskResult",
                    json={"clientKey": self.api_key, "taskId": task_id},
                )
                r.raise_for_status()
                data = r.json()
                if data.get("errorId"):
                    raise RuntimeError(
                        f"CapSolver getTaskResult failed: "
                        f"{data.get('errorDescription') or data}"
                    )
                if data.get("status") == "ready":
                    token = (data.get("solution") or {}).get(
                        "gRecaptchaResponse"
                    )
                    if not token:
                        raise RuntimeError(
                            f"CapSolver ready but no token: {data}"
                        )
                    return token
                time.sleep(2.0)

        raise RuntimeError("CapSolver timed out waiting for task result")


class TwoCaptcha(SolverService):
    """2Captcha (``api.2captcha.com``) integration skeleton.

    Bring your own ``api_key``. Uses 2Captcha's modern JSON ``/createTask`` +
    ``/getTaskResult`` endpoints. The request shape is real; the result-polling
    loop is left as a documented TODO.

    See: https://2captcha.com/api-docs
    """

    BASE_URL = "https://api.2captcha.com"

    # challenge kind -> (2Captcha task type, solution field holding the token)
    _TASK_TYPES = {
        "recaptcha_v3": ("RecaptchaV3TaskProxyless", "gRecaptchaResponse"),
        "recaptcha_v2": ("RecaptchaV2TaskProxyless", "gRecaptchaResponse"),
        "turnstile": ("TurnstileTaskProxyless", "token"),
        "hcaptcha": ("HCaptchaTaskProxyless", "gRecaptchaResponse"),
        "funcaptcha": ("FunCaptchaTaskProxyless", "token"),
    }

    def _task_for(self, challenge: "Challenge") -> "tuple[dict, str]":
        if challenge.kind not in self._TASK_TYPES:
            raise ValueError(f"2Captcha: unsupported challenge kind {challenge.kind!r}")
        ttype, field = self._TASK_TYPES[challenge.kind]
        task: dict = {"type": ttype, "websiteURL": challenge.url, "websiteKey": challenge.sitekey}
        if challenge.kind == "recaptcha_v3":
            task["pageAction"] = challenge.action
            task["minScore"] = challenge.min_score
        if challenge.kind == "turnstile":
            task["action"] = challenge.action
            if challenge.data:
                task["data"] = challenge.data
        if challenge.enterprise and challenge.kind in ("recaptcha_v3", "recaptcha_v2"):
            task["enterprise"] = 1
        return task, field

    def solve(
        self,
        sitekey: str,
        url: str,
        action: str = "submit",
        *,
        min_score: float = 0.5,
    ) -> str:
        import httpx

        # RecaptchaV3TaskProxyless for v3; use RecaptchaV2TaskProxyless for v2.
        task = {
            "type": "RecaptchaV3TaskProxyless",
            "websiteURL": url,
            "websiteKey": sitekey,
            "pageAction": action,
            "minScore": min_score,
        }
        payload = {"clientKey": self.api_key, "task": task}

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.BASE_URL}/createTask", json=payload)
            resp.raise_for_status()
            created = resp.json()
            if created.get("errorId"):
                raise RuntimeError(
                    f"2Captcha createTask failed: "
                    f"{created.get('errorDescription') or created}"
                )
            task_id = created.get("taskId")
            if not task_id:
                raise RuntimeError(f"2Captcha returned no taskId: {created}")

            # TODO(byok): poll /getTaskResult until status == 'ready', honouring
            # self.timeout, then return result['solution']['gRecaptchaResponse'].
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                r = client.post(
                    f"{self.BASE_URL}/getTaskResult",
                    json={"clientKey": self.api_key, "taskId": task_id},
                )
                r.raise_for_status()
                data = r.json()
                if data.get("errorId"):
                    raise RuntimeError(
                        f"2Captcha getTaskResult failed: "
                        f"{data.get('errorDescription') or data}"
                    )
                if data.get("status") == "ready":
                    token = (data.get("solution") or {}).get(
                        "gRecaptchaResponse"
                    )
                    if not token:
                        raise RuntimeError(
                            f"2Captcha ready but no token: {data}"
                        )
                    return token
                time.sleep(5.0)

        raise RuntimeError("2Captcha timed out waiting for task result")
