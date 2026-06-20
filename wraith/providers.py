"""First-class residential-proxy providers for Wraith.

WHY THIS EXISTS
---------------
The hardest WAAP failures Wraith faces are *reputation-of-IP* problems, not
fingerprint problems (see :class:`wraith.engine.WaapRateLimitedError` and the
``wraith.proxy`` module docstring). The fix for an HTTP **474/481** rate-limit
tier — or a silently tanked behavioral score after an exit IP has been hammered
— is a *rotating residential proxy*. ``wraith.proxy.ProxyPool`` is the rotation
primitive; this module is the glue that turns a provider account into the proxy
URL strings that pool (and ``engine.launch(proxy=...)`` /
``engine.clear_challenge(proxy_pool=...)``) consume.

DATAIMPULSE
-----------
`DataImpulse <https://dataimpulse.com>`_ is a **pay-per-GB residential** proxy
network. You authenticate to a single gateway host with
``username:password`` and steer the exit IP entirely through the *username*:
DataImpulse parses an enrichment block appended to the base username.

* **Gateway:** ``gw.dataimpulse.com``. Ports: ``823`` for HTTP/HTTPS, ``824``
  for SOCKS5.
* **Username enrichment:** the base username, then ``__`` (double underscore),
  then params joined by ``;``, each formatted ``key.value``. Recognised keys:

  =========  ====================================================
  ``cr``     country code, lowercase ISO-3166 alpha-2 (``us``, ``il``)
  ``city``   city slug (``newyork``)
  ``sessid`` sticky-session id — same exit IP for ~30 min
  =========  ====================================================

  Examples for base username ``acct123``::

      acct123                          # rotating, any country
      acct123__cr.il                   # rotating, country IL
      acct123__cr.il;sessid.profile01  # sticky (~30 min), country IL
      acct123__sessid.abc              # sticky, no country pin

  With **no** params it is just the bare base username (rotates per request).

* **Proxy URL:** ``<scheme>://<enriched_user>:<password>@gw.dataimpulse.com:<port>``
  where ``scheme``/``port`` are ``http``/``823`` (default, also ``https``/823)
  or ``socks5``/``824``.

A **base** username (no ``sessid``) rotates the exit IP on *every request*. Add
a ``sessid`` and the IP becomes **sticky** for the lifetime of that session id.
:meth:`DataImpulse.pool` exploits this: ``n`` *distinct* sticky session ids give
``n`` different sticky IPs that :class:`~wraith.proxy.ProxyPool` can rotate
across — exactly what ``clear_challenge(proxy_pool=...)`` wants when it has to
retry a 474/481/492 against a fresh exit IP.

GEOIP / IDENTITY CONSISTENCY
----------------------------
Pair any DataImpulse proxy with ``geoip=True`` (the default on
``engine.launch``): Camoufox derives a coherent timezone / locale /
``Accept-Language`` from the proxy *exit IP*, so a residential IL exit presents
as ``Asia/Jerusalem`` / ``he-IL`` rather than contradicting itself. A **sticky**
session keeps that derived identity stable for its whole ~30-minute lifetime;
a **rotating** endpoint re-derives it per IP, so prefer sticky when you need a
consistent identity across several navigations.

EXAMPLE
-------
::

    from wraith.providers import DataImpulse
    from wraith.engine import clear_challenge

    di = DataImpulse(country="il")            # creds from env / ~/.secrets
    # auto-rotate residential IL IPs on 474/481/492:
    sess = clear_challenge(
        "https://target.example/gated",
        proxy_pool=di.pool(5, country="il"),  # 5 distinct sticky IL exits
        geoip=True,
    )
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .proxy import ProxyPool

__all__ = ["DataImpulse", "DataImpulseAuthError"]


# DataImpulse gateway protocol -> port. HTTP and HTTPS share the 823 listener;
# SOCKS5 has its own on 824.
_DATAIMPULSE_PORTS: dict[str, int] = {"http": 823, "https": 823, "socks5": 824}

_DEFAULT_HOST = "gw.dataimpulse.com"


class DataImpulseAuthError(Exception):
    """Raised when a DataImpulse proxy URL is requested without credentials.

    Construction never raises (so ``import wraith`` and bare ``DataImpulse()``
    are always safe even with no account configured); this is raised lazily,
    only when a method that actually needs the ``username``/``password`` is
    called and one of them could not be resolved.
    """


def _strip_env_value(raw: str) -> str:
    """Normalise a value read from a ``KEY=value`` secrets-file line.

    Tolerates a surrounding pair of matching single/double quotes and trailing
    inline whitespace. (The optional ``export `` prefix is handled by the
    caller, which splits on the first ``=``.)
    """
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v


def _parse_secrets_file(path: Path, keys: "tuple[str, ...]") -> "dict[str, str]":
    """Best-effort parse of ``KEY=value`` lines from a shell-style secrets file.

    Tolerates a leading ``export `` and quoted values. Returns only the
    requested ``keys`` that are present and non-empty. Any read error (missing
    file, permissions) yields an empty mapping rather than raising — credential
    resolution must degrade gracefully.
    """
    found: dict[str, str] = {}
    try:
        text = path.expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return found

    wanted = set(keys)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if key in wanted:
            value = _strip_env_value(value)
            if value:
                found[key] = value
    return found


class DataImpulse:
    """A DataImpulse residential-proxy account → proxy URL strings & pools.

    Credentials are resolved at construction time, most-specific first:

    1. explicit ``username`` / ``password`` arguments;
    2. environment ``DATAIMPULSE_USERNAME`` / ``DATAIMPULSE_PASSWORD``;
    3. (only if ``from_env``) ``KEY=value`` lines in ``secrets_file``
       (default ``~/.secrets``), tolerating an ``export `` prefix and quotes.

    A credential that cannot be resolved is stored as ``None``; **construction
    never raises**. :class:`DataImpulseAuthError` is raised lazily, only when a
    proxy URL is actually requested (so importing/constructing without an
    account is harmless).

    Args:
        username: account username (the *base*, before any enrichment block).
        password: account password.
        country: default country code (lowercase ISO alpha-2, e.g. ``"il"``)
            applied to every URL unless overridden per-call.
        city: default city slug (e.g. ``"newyork"``), overridable per-call.
        protocol: ``"http"`` (default), ``"https"`` (both → port 823) or
            ``"socks5"`` (→ port 824).
        host: gateway host (default ``gw.dataimpulse.com``).
        from_env: when ``True`` (default), fall back to environment variables
            and ``secrets_file`` for any unset credential. When ``False``, only
            explicit args + environment variables are consulted (no file read).
        secrets_file: path to a shell-style secrets file to parse for
            ``DATAIMPULSE_USERNAME=`` / ``DATAIMPULSE_PASSWORD=`` lines.
    """

    ENV_USERNAME = "DATAIMPULSE_USERNAME"
    ENV_PASSWORD = "DATAIMPULSE_PASSWORD"

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        *,
        country: Optional[str] = None,
        city: Optional[str] = None,
        protocol: str = "http",
        host: str = _DEFAULT_HOST,
        from_env: bool = True,
        secrets_file: str = "~/.secrets",
    ) -> None:
        protocol = (protocol or "http").lower()
        if protocol not in _DATAIMPULSE_PORTS:
            raise ValueError(
                f"unknown protocol {protocol!r}; "
                f"expected one of {sorted(_DATAIMPULSE_PORTS)}"
            )
        self.protocol = protocol
        self.host = host or _DEFAULT_HOST
        self.country = country.lower() if country else None
        self.city = city
        self.from_env = from_env
        self.secrets_file = secrets_file

        self.username, self.password = self._resolve_credentials(
            username, password, from_env=from_env, secrets_file=secrets_file
        )

    # ------------------------------------------------------------------ #
    # Credential resolution
    # ------------------------------------------------------------------ #
    @classmethod
    def _resolve_credentials(
        cls,
        username: Optional[str],
        password: Optional[str],
        *,
        from_env: bool,
        secrets_file: str,
    ) -> "tuple[Optional[str], Optional[str]]":
        """Resolve (username, password) explicit > env > secrets-file."""
        # 1) explicit args win.
        user = username.strip() if isinstance(username, str) and username.strip() else None
        pw = password if isinstance(password, str) and password else None

        # 2) environment variables.
        if user is None:
            env_user = os.environ.get(cls.ENV_USERNAME)
            if env_user and env_user.strip():
                user = env_user.strip()
        if pw is None:
            env_pw = os.environ.get(cls.ENV_PASSWORD)
            if env_pw:
                pw = env_pw

        # 3) secrets file (only if we still need something and from_env is on).
        if from_env and (user is None or pw is None):
            parsed = _parse_secrets_file(
                Path(secrets_file), (cls.ENV_USERNAME, cls.ENV_PASSWORD)
            )
            if user is None:
                user = parsed.get(cls.ENV_USERNAME) or None
            if pw is None:
                pw = parsed.get(cls.ENV_PASSWORD) or None

        return user, pw

    def _require_credentials(self) -> "tuple[str, str]":
        """Return (username, password) or raise :class:`DataImpulseAuthError`."""
        if not self.username or not self.password:
            missing = []
            if not self.username:
                missing.append(self.ENV_USERNAME)
            if not self.password:
                missing.append(self.ENV_PASSWORD)
            raise DataImpulseAuthError(
                "DataImpulse credentials are not configured: missing "
                f"{', '.join(missing)}. Pass username=/password= explicitly, set "
                f"the {self.ENV_USERNAME}/{self.ENV_PASSWORD} environment "
                f"variables, or add them to {self.secrets_file}."
            )
        return self.username, self.password

    # ------------------------------------------------------------------ #
    # URL assembly
    # ------------------------------------------------------------------ #
    @property
    def _port(self) -> int:
        """Gateway port derived from :attr:`protocol` (823 http/https, 824 socks5)."""
        return _DATAIMPULSE_PORTS[self.protocol]

    def _enriched_username(
        self,
        country: Optional[str] = None,
        city: Optional[str] = None,
        sessid: Optional[str] = None,
    ) -> str:
        """Build the DataImpulse base+enrichment username.

        Per-call ``country``/``city`` override the instance defaults; passing an
        empty string (``""``) explicitly *unsets* that field for this call.
        Params are emitted in a stable order (``cr``, ``city``, ``sessid``) as
        ``key.value`` joined by ``;`` after a leading ``__``. With no params the
        bare base username is returned (rotating endpoint).
        """
        base, _ = self._require_credentials()

        # ``None`` -> fall back to instance default; ``""`` -> explicit unset.
        cr = self.country if country is None else (country.lower() if country else None)
        cty = self.city if city is None else (city or None)

        params: list[str] = []
        if cr:
            params.append(f"cr.{cr}")
        if cty:
            params.append(f"city.{cty}")
        if sessid:
            params.append(f"sessid.{sessid}")

        if not params:
            return base
        return base + "__" + ";".join(params)

    def _build_url(
        self,
        *,
        country: Optional[str] = None,
        city: Optional[str] = None,
        sessid: Optional[str] = None,
    ) -> str:
        _, pw = self._require_credentials()
        user = self._enriched_username(country=country, city=city, sessid=sessid)
        return f"{self.protocol}://{user}:{pw}@{self.host}:{self._port}"

    def rotating(
        self, *, country: Optional[str] = None, city: Optional[str] = None
    ) -> str:
        """Proxy URL for a **rotating** exit IP (no ``sessid`` → new IP per request).

        Per-call ``country``/``city`` override the instance defaults.
        """
        return self._build_url(country=country, city=city, sessid=None)

    def sticky(
        self,
        session_id: str,
        *,
        country: Optional[str] = None,
        city: Optional[str] = None,
    ) -> str:
        """Proxy URL for a **sticky** exit IP pinned to ``session_id`` (~30 min).

        The same ``session_id`` yields the same exit IP for the session
        lifetime; distinct ids yield distinct IPs. Per-call ``country``/``city``
        override the instance defaults.
        """
        if not session_id:
            raise ValueError("sticky() requires a non-empty session_id")
        return self._build_url(country=country, city=city, sessid=str(session_id))

    def pool(
        self,
        n: int = 5,
        *,
        country: Optional[str] = None,
        city: Optional[str] = None,
        sticky: bool = True,
        strategy: str = "round_robin",
    ) -> ProxyPool:
        """Build a :class:`~wraith.proxy.ProxyPool` of ``n`` DataImpulse exits.

        With ``sticky=True`` (default) this mints ``n`` **distinct** sticky
        sessions (ids ``wraith-0`` .. ``wraith-(n-1)``), i.e. ``n`` different
        exit IPs the pool can rotate across — exactly what
        ``clear_challenge(proxy_pool=...)`` needs to retry a 474/481/492 against
        a fresh residential IP. With ``sticky=False`` it returns ``n`` copies of
        the rotating endpoint (which collapse to a single entry, since
        ``ProxyPool`` de-dupes — every request through it already rotates the IP
        gateway-side).

        Args:
            n: number of exits (must be ``>= 1``).
            country / city: applied to every exit; override instance defaults.
            sticky: distinct sticky sessions (``True``) vs. rotating copies.
            strategy: forwarded to :class:`~wraith.proxy.ProxyPool`.
        """
        if n < 1:
            raise ValueError(f"pool size must be >= 1, got {n}")
        # Validate creds eagerly so an empty/misconfigured account fails here
        # rather than producing a silently empty pool.
        self._require_credentials()

        if sticky:
            urls = [
                self.sticky(f"wraith-{i}", country=country, city=city)
                for i in range(n)
            ]
        else:
            urls = [
                self.rotating(country=country, city=city) for _ in range(n)
            ]
        return ProxyPool(urls, strategy=strategy)

    # ------------------------------------------------------------------ #
    # Dunders
    # ------------------------------------------------------------------ #
    def __repr__(self) -> str:
        user = self.username or "<unset>"
        pw = "***" if self.password else "<unset>"
        return (
            f"DataImpulse(username={user!r}, password={pw}, "
            f"protocol={self.protocol!r}, host={self.host!r}, "
            f"country={self.country!r}, city={self.city!r})"
        )
