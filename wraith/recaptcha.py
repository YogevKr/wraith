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
from typing import Any

__all__ = [
    "harvest_token",
    "score",
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


class CapSolver(SolverService):
    """CapSolver (``api.capsolver.com``) integration skeleton.

    Bring your own ``api_key``. Uses CapSolver's ``/createTask`` +
    ``/getTaskResult`` endpoints. The request shape is real; the result-polling
    loop is left as a documented TODO so this never silently pretends to solve.

    See: https://docs.capsolver.com/
    """

    BASE_URL = "https://api.capsolver.com"

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
