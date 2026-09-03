"""First-class residential-proxy providers for Wraith (DataImpulse, anyIP).

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

ANYIP
-----
`anyIP <https://anyip.io>`_ is a **pay-per-GB residential + mobile** proxy
network with the same shape: one gateway, ``username:password`` auth, and every
targeting/session knob expressed as *flags appended to the username*.

* **Gateway:** ``portal.anyip.io`` (regional entry points
  ``portal-na.anyip.io`` / ``portal-eu.anyip.io`` / ``portal-as.anyip.io``).
  Ports ``1080`` (default) or ``443``; HTTP, HTTPS **and SOCKS5 all share the
  same port**.
* **Username flags:** the account id ``user_<id>``, then ``,``-separated
  ``key_value`` flags (anyIP also accepts ``|``; Wraith emits ``,`` because it
  is a legal URL sub-delimiter, so the proxy URL string stays valid unquoted).
  Recognised flags:

  ===================  ==================================================
  ``type_<net>``       ``residential`` or ``mobile``; omit = mixed pool
  ``country_<CC>``     ISO-3166 alpha-2, **uppercase** (``US``, ``IL``)
  ``region_<slug>``    state/region slug, lowercase (``texas``); needs country
  ``city_<slug>``      city slug, lowercase (``dallas``); needs region
  ``asn_<n>``          numeric ASN (``7922``)
  ``pool_<name>``      regional pool (``europe``, ``mena``, ``western`` ...)
  ``session_<id>``     sticky session; ``[A-Za-z0-9_]``, max 32 chars
  ``sesstime_<min>``   sticky lifetime in minutes, ``1``..``10080`` (7 days)
  ``sessreplace_false`` do **not** swap in a new IP if the peer drops
  ``sessasn_strict``   a replacement peer must keep the same ASN
  ===================  ==================================================

  Examples for account id ``user_ab12``::

      user_ab12                                   # rotating, mixed, any country
      user_ab12,type_mobile,country_IL            # rotating IL mobile
      user_ab12,country_IL,session_profile01      # sticky (7 days max)
      user_ab12,country_US,region_texas,city_dallas,session_x,sesstime_30

* **Proxy URL:** ``<scheme>://<user_flags>:<password>@portal.anyip.io:1080``.

Without a ``session_`` flag anyIP routes **every request through a fresh IP**;
with one, the IP is sticky for up to ``sesstime`` minutes (default up to 7
days, availability permitting). :meth:`AnyIP.pool` mints ``n`` distinct sticky
sessions for :class:`~wraith.proxy.ProxyPool` exactly like
:meth:`DataImpulse.pool`.

GEOIP / IDENTITY CONSISTENCY
----------------------------
Pair any DataImpulse or anyIP proxy with ``geoip=True`` (the default on
``engine.launch``): Camoufox derives a coherent timezone / locale /
``Accept-Language`` from the proxy *exit IP*, so a residential IL exit presents
as ``Asia/Jerusalem`` / ``he-IL`` rather than contradicting itself. A **sticky**
session keeps that derived identity stable for its whole ~30-minute lifetime;
a **rotating** endpoint re-derives it per IP, so prefer sticky when you need a
consistent identity across several navigations.

EXAMPLE
-------
::

    from wraith.providers import AnyIP, DataImpulse
    from wraith.engine import clear_challenge

    di = DataImpulse(country="il")            # creds from env / ~/.secrets
    # auto-rotate residential IL IPs on 474/481/492:
    sess = clear_challenge(
        "https://target.example/gated",
        proxy_pool=di.pool(5, country="il"),  # 5 distinct sticky IL exits
        geoip=True,
    )

    ai = AnyIP(country="il", network="mobile")   # ANYIP_USERNAME/PASSWORD
    sess = clear_challenge(
        "https://target.example/gated",
        proxy_pool=ai.pool(5, minutes=30),    # 5 sticky IL mobile exits, 30 min
    )

Credentials for either provider may come from a **command** instead of a
value — ``AnyIP(password_cmd="op read op://vault/anyip/credential")``, or
``ANYIP_PASSWORD_CMD="op read op://vault/anyip/credential"`` in the
environment / secrets file. See :mod:`wraith.credentials` for the full order.

Both providers hand back plain proxy URL *strings*; ``engine.launch`` converts
them to the ``{"server", "username", "password"}`` dict Playwright/Camoufox
expect (see :func:`wraith.proxy.to_playwright_proxy`).
"""

from __future__ import annotations

import re
from typing import Optional

from .credentials import SecretCommandError, resolve_secret
from .proxy import ProxyPool

__all__ = [
    "AnyIP",
    "AnyIPAuthError",
    "DataImpulse",
    "DataImpulseAuthError",
    "PROVIDER_LITERALS",
    "SecretCommandError",
    "resolve_proxy_spec",
]


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


def _resolve_provider_credentials(
    username: Optional[str],
    password: Optional[str],
    *,
    env_username: str,
    env_password: str,
    from_env: bool,
    secrets_file: str,
    username_cmd: Optional[str] = None,
    password_cmd: Optional[str] = None,
) -> "tuple[Optional[str], Optional[str]]":
    """Resolve ``(username, password)`` for a provider via :func:`resolve_secret`.

    Per credential, first non-empty wins: explicit value → explicit ``*_cmd``
    command → env ``<VAR>`` → env ``<VAR>_CMD`` (run) → ``secrets_file``
    ``<VAR>=`` → ``secrets_file`` ``<VAR>_CMD=`` (run). The file tiers are
    skipped when ``from_env`` is ``False``. A credential that cannot be found
    is ``None`` — callers raise their provider-specific auth error lazily. A
    command that fails raises :class:`SecretCommandError` immediately.
    """
    file_ = secrets_file if from_env else None
    explicit_user = username.strip() if isinstance(username, str) and username.strip() else None
    explicit_pw = password if isinstance(password, str) and password else None

    user = resolve_secret(
        env_username, value=explicit_user, command=username_cmd, secrets_file=file_
    )
    if user is not None:
        user = user.strip() or None
    pw = resolve_secret(
        env_password, value=explicit_pw, command=password_cmd, secrets_file=file_
    )
    return user, pw or None


class DataImpulse:
    """A DataImpulse residential-proxy account → proxy URL strings & pools.

    Credentials are resolved at construction time, most-specific first (see
    :mod:`wraith.credentials`):

    1. explicit ``username`` / ``password`` arguments;
    2. explicit ``username_cmd`` / ``password_cmd`` shell commands whose
       stdout is the value (``"op read op://vault/dataimpulse/credential"``);
    3. environment ``DATAIMPULSE_USERNAME`` / ``DATAIMPULSE_PASSWORD``;
    4. environment ``DATAIMPULSE_USERNAME_CMD`` / ``DATAIMPULSE_PASSWORD_CMD``
       — commands, run the same way;
    5. (only if ``from_env``) ``KEY=value`` or ``KEY_CMD=command`` lines in
       ``secrets_file`` (default ``~/.secrets``), tolerating an ``export ``
       prefix and quotes.

    A credential that cannot be resolved is stored as ``None``; **construction
    never raises**. :class:`DataImpulseAuthError` is raised lazily, only when a
    proxy URL is actually requested (so importing/constructing without an
    account is harmless).

    Args:
        username: account username (the *base*, before any enrichment block).
        password: account password.
        username_cmd / password_cmd: shell commands that print the credential
            (used when the explicit value is absent; beat the environment).
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
        username_cmd: Optional[str] = None,
        password_cmd: Optional[str] = None,
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
            username,
            password,
            from_env=from_env,
            secrets_file=secrets_file,
            username_cmd=username_cmd,
            password_cmd=password_cmd,
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
        username_cmd: Optional[str] = None,
        password_cmd: Optional[str] = None,
    ) -> "tuple[Optional[str], Optional[str]]":
        """Resolve (username, password) explicit > command > env > env _CMD > file."""
        return _resolve_provider_credentials(
            username,
            password,
            env_username=cls.ENV_USERNAME,
            env_password=cls.ENV_PASSWORD,
            from_env=from_env,
            secrets_file=secrets_file,
            username_cmd=username_cmd,
            password_cmd=password_cmd,
        )

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


# =========================================================================== #
# anyIP
# =========================================================================== #

_ANYIP_DEFAULT_HOST = "portal.anyip.io"
_ANYIP_DEFAULT_PORT = 1080
# HTTP, HTTPS and SOCKS5 all share the same listener on anyIP (1080 or 443).
_ANYIP_PROTOCOLS = ("http", "https", "socks5")
_ANYIP_NETWORKS = ("residential", "mobile")
# The gateway rejects session names > 32 chars or with characters outside
# ``a-Z 0-9`` (``bad_attributes_session_name_*`` 407s). anyIP's own examples
# use ``_`` (``session_ig_client1``), so we allow it too.
_ANYIP_SESSION_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_ANYIP_SESSTIME_MIN = 1
_ANYIP_SESSTIME_MAX = 10_080  # one week, in minutes
_ANYIP_USERNAME_PREFIX = "user_"
# anyIP accepts ``,`` or ``|`` between flags. ``,`` is an RFC 3986 sub-delim
# (legal in URL userinfo unescaped); ``|`` is not — so we always emit ``,``.
_ANYIP_SEP = ","
_ANYIP_FLAG_SEPARATORS = (",", "|")


class AnyIPAuthError(Exception):
    """Raised when an anyIP proxy URL is requested without credentials.

    Construction never raises for *missing* credentials (so ``import wraith``
    and bare ``AnyIP()`` are always safe with no account configured); this is
    raised lazily, only when a method that actually needs the
    ``username``/``password`` is called and one of them could not be resolved.
    """


def _anyip_slug(value: Optional[str]) -> Optional[str]:
    """Normalise a region/city/pool name to anyIP's slug form.

    anyIP spells locations lowercase with whitespace removed (``newyork``,
    ``losangeles``); this lowercases and strips *all* whitespace so
    ``"New York"`` and ``"newyork"`` both produce ``newyork``. Empty → ``None``.
    """
    if value is None:
        return None
    slug = "".join(str(value).lower().split())
    return slug or None


def _anyip_network(value: Optional[str]) -> Optional[str]:
    """Validate a ``network`` value: ``"residential"`` | ``"mobile"`` | ``None`` (mixed)."""
    if value is None or value == "":
        return None
    net = str(value).strip().lower()
    if net not in _ANYIP_NETWORKS:
        raise ValueError(
            f"unknown anyIP network {value!r}; expected one of "
            f"{list(_ANYIP_NETWORKS)} (or None for the mixed pool)"
        )
    return net


def _anyip_country(value: Optional[str]) -> Optional[str]:
    """Normalise a country code to anyIP's **uppercase** ISO alpha-2 form."""
    if value is None or value == "":
        return None
    cc = str(value).strip().upper()
    if len(cc) != 2 or not cc.isalpha():
        raise ValueError(
            f"invalid anyIP country {value!r}; expected a 2-letter ISO code (e.g. 'US')"
        )
    return cc


def _anyip_asn(value: "Optional[int | str]") -> Optional[int]:
    """Validate an ASN: a positive integer (``7922`` or ``"7922"``/``"AS7922"``)."""
    if value is None or value == "":
        return None
    raw = str(value).strip()
    if raw.upper().startswith("AS"):
        raw = raw[2:]
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError(f"invalid anyIP asn {value!r}; expected a positive integer")
    return int(raw)


def _anyip_username(value: Optional[str]) -> Optional[str]:
    """Normalise the account id to the bare ``user_<id>`` base username.

    Accepts ``"ab12"`` or ``"user_ab12"`` (the prefix is added if missing).
    Rejects a *flagged* username — the whole line the anyIP dashboard offers
    to copy (``user_ab12,type_residential,country_CA``) — because silently
    keeping or dropping those flags would both be wrong; targeting belongs in
    the :class:`AnyIP` constructor / per-call arguments instead.
    """
    if value is None:
        return None
    user = value.strip()
    if not user:
        return None
    if any(sep in user for sep in _ANYIP_FLAG_SEPARATORS):
        raise ValueError(
            "anyIP username must be the bare account id ('user_<id>'), not the "
            f"flagged line from the dashboard ({user.split(',')[0]!r},...); "
            "express targeting via AnyIP(network=, country=, ...) instead."
        )
    if not user.startswith(_ANYIP_USERNAME_PREFIX):
        user = _ANYIP_USERNAME_PREFIX + user
    return user


class AnyIP:
    """An anyIP residential/mobile-proxy account → proxy URL strings & pools.

    Same contract as :class:`DataImpulse`: credentials resolve at construction
    (explicit args > ``username_cmd``/``password_cmd`` commands >
    ``ANYIP_USERNAME``/``ANYIP_PASSWORD`` env > ``ANYIP_*_CMD`` env commands >
    ``KEY=`` / ``KEY_CMD=`` lines in ``secrets_file`` — see
    :mod:`wraith.credentials`), **missing credentials never raise at
    construction** — :class:`AnyIPAuthError` is raised lazily when a URL is
    actually requested. Every targeting knob becomes a ``,``-separated flag in
    the proxy *username* (see the module docstring for the flag table).

    Args:
        username: the anyIP account id — ``"user_ab12"`` or just ``"ab12"``.
            Must **not** include flags (see :func:`_anyip_username`).
        password: account password.
        username_cmd / password_cmd: shell commands that print the credential
            (e.g. ``password_cmd="op read op://vault/anyip/credential"``); used when the
            explicit value is absent and consulted before the environment.
        network: ``"residential"``, ``"mobile"``, or ``None`` (default) for
            anyIP's mixed pool (it then picks residential *or* mobile per IP).
            Mobile exits carry the highest trust score but are slower.
        country: default country (ISO alpha-2, any case; emitted uppercase).
        region: default state/region slug (``"texas"``); **requires country**.
        city: default city slug (``"dallas"``); **requires region**.
        asn: default ASN (``7922`` / ``"AS7922"``) to pin an ISP/carrier.
        pool_name: default regional pool (``"europe"``, ``"mena"``, ...).
        protocol: ``"http"`` (default), ``"https"`` or ``"socks5"`` — all three
            share the same gateway port.
        host: gateway host (default ``portal.anyip.io``; regional entry
            points are ``portal-na`` / ``portal-eu`` / ``portal-as``).
        port: gateway port — ``1080`` (default) or ``443``.
        from_env: when ``True`` (default), fall back to the environment and
            ``secrets_file`` for any unset credential.
        secrets_file: shell-style file with ``ANYIP_USERNAME=`` /
            ``ANYIP_PASSWORD=`` lines (default ``~/.secrets``).

    Per-call ``network``/``country``/``region``/``city``/``asn``/``pool_name``
    arguments on :meth:`rotating` / :meth:`sticky` / :meth:`pool` override the
    instance defaults; passing ``""`` explicitly *unsets* that field for the
    call. anyIP's dependency chain (city → region → country) is validated
    locally so a bad combination raises ``ValueError`` here instead of an HTTP
    407 ``city_region_is_missing`` at connect time.
    """

    ENV_USERNAME = "ANYIP_USERNAME"
    ENV_PASSWORD = "ANYIP_PASSWORD"

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        *,
        username_cmd: Optional[str] = None,
        password_cmd: Optional[str] = None,
        network: Optional[str] = None,
        country: Optional[str] = None,
        region: Optional[str] = None,
        city: Optional[str] = None,
        asn: "Optional[int | str]" = None,
        pool_name: Optional[str] = None,
        protocol: str = "http",
        host: str = _ANYIP_DEFAULT_HOST,
        port: int = _ANYIP_DEFAULT_PORT,
        from_env: bool = True,
        secrets_file: str = "~/.secrets",
    ) -> None:
        protocol = (protocol or "http").lower()
        if protocol not in _ANYIP_PROTOCOLS:
            raise ValueError(
                f"unknown protocol {protocol!r}; expected one of {list(_ANYIP_PROTOCOLS)}"
            )
        self.protocol = protocol
        self.host = host or _ANYIP_DEFAULT_HOST
        self.port = int(port)
        self.network = _anyip_network(network)
        self.country = _anyip_country(country)
        self.region = _anyip_slug(region)
        self.city = _anyip_slug(city)
        self.asn = _anyip_asn(asn)
        self.pool_name = _anyip_slug(pool_name)
        self.from_env = from_env
        self.secrets_file = secrets_file

        user, pw = _resolve_provider_credentials(
            username,
            password,
            env_username=self.ENV_USERNAME,
            env_password=self.ENV_PASSWORD,
            from_env=from_env,
            secrets_file=secrets_file,
            username_cmd=username_cmd,
            password_cmd=password_cmd,
        )
        self.username = _anyip_username(user)
        self.password = pw

    # ------------------------------------------------------------------ #
    # Credentials
    # ------------------------------------------------------------------ #
    def _require_credentials(self) -> "tuple[str, str]":
        """Return (username, password) or raise :class:`AnyIPAuthError`."""
        if not self.username or not self.password:
            missing = []
            if not self.username:
                missing.append(self.ENV_USERNAME)
            if not self.password:
                missing.append(self.ENV_PASSWORD)
            raise AnyIPAuthError(
                "anyIP credentials are not configured: missing "
                f"{', '.join(missing)}. Pass username=/password= explicitly, set "
                f"the {self.ENV_USERNAME}/{self.ENV_PASSWORD} environment "
                f"variables, or add them to {self.secrets_file}."
            )
        return self.username, self.password

    # ------------------------------------------------------------------ #
    # Username / URL assembly
    # ------------------------------------------------------------------ #
    def _targeting_flags(
        self,
        *,
        network: Optional[str],
        country: Optional[str],
        region: Optional[str],
        city: Optional[str],
        asn: "Optional[int | str]",
        pool_name: Optional[str],
    ) -> list[str]:
        """Resolve per-call overrides against instance defaults → flag list.

        ``None`` → instance default; ``""`` → explicitly unset for this call.
        Emitted in a stable order: type, country, region, city, asn, pool.
        """
        net = self.network if network is None else _anyip_network(network)
        cc = self.country if country is None else _anyip_country(country)
        reg = self.region if region is None else _anyip_slug(region)
        cty = self.city if city is None else _anyip_slug(city)
        asn_n = self.asn if asn is None else _anyip_asn(asn)
        pool_ = self.pool_name if pool_name is None else _anyip_slug(pool_name)

        # anyIP's dependency chain: city needs region, region needs country.
        if reg and not cc:
            raise ValueError("anyIP region targeting requires a country")
        if cty and not reg:
            raise ValueError("anyIP city targeting requires a region (and country)")

        flags: list[str] = []
        if net:
            flags.append(f"type_{net}")
        if cc:
            flags.append(f"country_{cc}")
        if reg:
            flags.append(f"region_{reg}")
        if cty:
            flags.append(f"city_{cty}")
        if asn_n:
            flags.append(f"asn_{asn_n}")
        if pool_:
            flags.append(f"pool_{pool_}")
        return flags

    @staticmethod
    def _session_flags(
        session_id: str,
        *,
        minutes: Optional[int],
        replace: bool,
        same_asn: bool,
    ) -> list[str]:
        """Build the sticky-session flags, validating what the gateway would 407 on."""
        sid = str(session_id) if session_id is not None else ""
        if not _ANYIP_SESSION_RE.match(sid):
            raise ValueError(
                f"invalid anyIP session id {session_id!r}: use 1-32 characters "
                "from [A-Za-z0-9_]"
            )
        flags = [f"session_{sid}"]
        if minutes is not None:
            mins = int(minutes)
            if not _ANYIP_SESSTIME_MIN <= mins <= _ANYIP_SESSTIME_MAX:
                raise ValueError(
                    f"anyIP session minutes must be {_ANYIP_SESSTIME_MIN}.."
                    f"{_ANYIP_SESSTIME_MAX} (one week), got {minutes!r}"
                )
            flags.append(f"sesstime_{mins}")
        if not replace:
            flags.append("sessreplace_false")
        if same_asn:
            flags.append("sessasn_strict")
        return flags

    def _build_url(self, flags: list[str]) -> str:
        base, pw = self._require_credentials()
        user = base if not flags else base + _ANYIP_SEP + _ANYIP_SEP.join(flags)
        return f"{self.protocol}://{user}:{pw}@{self.host}:{self.port}"

    def rotating(
        self,
        *,
        network: Optional[str] = None,
        country: Optional[str] = None,
        region: Optional[str] = None,
        city: Optional[str] = None,
        asn: "Optional[int | str]" = None,
        pool_name: Optional[str] = None,
    ) -> str:
        """Proxy URL for a **rotating** exit (no ``session_`` → new IP per request).

        Per-call targeting arguments override the instance defaults.
        """
        flags = self._targeting_flags(
            network=network, country=country, region=region, city=city,
            asn=asn, pool_name=pool_name,
        )
        return self._build_url(flags)

    def sticky(
        self,
        session_id: str,
        *,
        minutes: Optional[int] = None,
        replace: bool = True,
        same_asn: bool = False,
        network: Optional[str] = None,
        country: Optional[str] = None,
        region: Optional[str] = None,
        city: Optional[str] = None,
        asn: "Optional[int | str]" = None,
        pool_name: Optional[str] = None,
    ) -> str:
        """Proxy URL for a **sticky** exit pinned to ``session_id``.

        The same ``session_id`` keeps the same exit IP for up to ``minutes``
        (``sesstime_``; ``1``..``10080``; anyIP's default is up to 7 days,
        availability permitting). Distinct ids yield distinct IPs.

        Args:
            session_id: 1-32 chars from ``[A-Za-z0-9_]``.
            minutes: sticky lifetime in minutes (``sesstime_<n>``); ``None``
                leaves anyIP's default.
            replace: ``False`` emits ``sessreplace_false`` — if the peer drops,
                fail with ``peer_not_found`` instead of swapping in a new IP.
                Use when continuity matters more than availability.
            same_asn: ``True`` emits ``sessasn_strict`` — a replacement peer
                must keep the same ISP/ASN.
            network / country / region / city / asn / pool_name: per-call
                targeting overrides (see :meth:`rotating`).
        """
        flags = self._targeting_flags(
            network=network, country=country, region=region, city=city,
            asn=asn, pool_name=pool_name,
        )
        flags += self._session_flags(
            session_id, minutes=minutes, replace=replace, same_asn=same_asn
        )
        return self._build_url(flags)

    def pool(
        self,
        n: int = 5,
        *,
        sticky: bool = True,
        minutes: Optional[int] = None,
        same_asn: bool = False,
        strategy: str = "round_robin",
        network: Optional[str] = None,
        country: Optional[str] = None,
        region: Optional[str] = None,
        city: Optional[str] = None,
        asn: "Optional[int | str]" = None,
        pool_name: Optional[str] = None,
    ) -> ProxyPool:
        """Build a :class:`~wraith.proxy.ProxyPool` of ``n`` anyIP exits.

        With ``sticky=True`` (default) this mints ``n`` **distinct** sticky
        sessions (ids ``wraith0`` .. ``wraith<n-1>``) — ``n`` different exit
        IPs the pool can rotate across when ``clear_challenge(proxy_pool=...)``
        retries a 474/481/492. With ``sticky=False`` it returns the rotating
        endpoint (``n`` identical copies collapse to one entry, since every
        request through it already rotates gateway-side).

        Args:
            n: number of exits (``>= 1``).
            sticky: distinct sticky sessions (``True``) vs. the rotating endpoint.
            minutes: sticky lifetime per session (``sesstime_``), optional.
            same_asn: emit ``sessasn_strict`` on every sticky session.
            strategy: forwarded to :class:`~wraith.proxy.ProxyPool`.
            network / country / region / city / asn / pool_name: applied to
                every exit; override the instance defaults.
        """
        if n < 1:
            raise ValueError(f"pool size must be >= 1, got {n}")
        # Fail here on a missing account rather than producing an empty pool.
        self._require_credentials()

        targeting = dict(
            network=network, country=country, region=region, city=city,
            asn=asn, pool_name=pool_name,
        )
        if sticky:
            urls = [
                self.sticky(f"wraith{i}", minutes=minutes, same_asn=same_asn, **targeting)
                for i in range(n)
            ]
        else:
            urls = [self.rotating(**targeting) for _ in range(n)]
        return ProxyPool(urls, strategy=strategy)

    # ------------------------------------------------------------------ #
    # Dunders
    # ------------------------------------------------------------------ #
    def __repr__(self) -> str:
        user = self.username or "<unset>"
        pw = "***" if self.password else "<unset>"
        return (
            f"AnyIP(username={user!r}, password={pw}, protocol={self.protocol!r}, "
            f"host={self.host!r}, port={self.port}, network={self.network!r}, "
            f"country={self.country!r}, region={self.region!r}, city={self.city!r}, "
            f"asn={self.asn!r}, pool_name={self.pool_name!r})"
        )


# =========================================================================== #
# Shared "proxy spec" resolution (CLI flags / MCP env → proxy URL)
# =========================================================================== #

#: ``--proxy`` / ``WRAITH_PROXY`` literals that select a provider instead of a URL.
PROVIDER_LITERALS = ("dataimpulse", "anyip")


def resolve_proxy_spec(
    spec: Optional[str],
    *,
    dataimpulse: bool = False,
    anyip: bool = False,
    country: Optional[str] = None,
    network: Optional[str] = None,
    username_cmd: Optional[str] = None,
    password_cmd: Optional[str] = None,
) -> "tuple[Optional[str], Optional[str]]":
    """Turn a proxy *spec* into ``(proxy_url, provider_label)``.

    One resolution path shared by the ``wraith`` CLI (``--proxy`` /
    ``--dataimpulse`` / ``--anyip`` flags) and the MCP server (``WRAITH_PROXY``
    / ``WRAITH_PROXY_COUNTRY`` / ``WRAITH_PROXY_NETWORK`` env):

    * ``spec`` is a literal ``"dataimpulse"`` / ``"anyip"`` (case-insensitive),
      or the matching boolean is set → build that provider's **rotating** exit
      honoring ``country`` (both) and ``network`` (anyIP only), with optional
      ``username_cmd`` / ``password_cmd`` secret commands. Returns
      ``(url, "DataImpulse" | "anyIP")``.
    * any other non-empty ``spec`` → passed through verbatim as
      ``(spec, None)``.
    * nothing selected → ``(None, None)``.

    Raises ``ValueError`` when both providers are selected, and lets
    :class:`DataImpulseAuthError` / :class:`AnyIPAuthError` /
    :class:`SecretCommandError` / targeting ``ValueError`` propagate — callers
    decide how to report them.
    """
    literal = spec.strip().lower() if isinstance(spec, str) else ""
    want_di = bool(dataimpulse) or literal == "dataimpulse"
    want_ai = bool(anyip) or literal == "anyip"
    if want_di and want_ai:
        raise ValueError("choose one proxy provider (dataimpulse or anyip)")
    if not (want_di or want_ai):
        return (spec.strip() if isinstance(spec, str) and spec.strip() else None), None

    cmds = dict(username_cmd=username_cmd, password_cmd=password_cmd)
    if want_ai:
        return AnyIP(country=country, network=network, **cmds).rotating(), "anyIP"
    return DataImpulse(country=country, **cmds).rotating(country=country), "DataImpulse"
