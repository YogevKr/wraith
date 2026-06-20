"""Identity borrowing — Wraith's signature feature.

THE PROBLEM (reCAPTCHA-v3 reputation, empirically verified vs. EL AL's
Reblaze/Link11 + reCAPTCHA-v3 + Akamai + SiteMinder stack):

    reCAPTCHA-v3 has no "solver". It is a *reputation* score in [0.0, 1.0]
    derived from your Google account cookies, your aged browsing history,
    and your IP. A fresh, automated browser profile scores ~0.1-0.3 (read:
    "bot") no matter how good the stealth engine is, because it has no
    history to vouch for it. A real human's warmed-up browser scores ~0.9.
    You cannot fake reputation, and there is no public bypass for the
    Reblaze/Link11 WAAP that fronts these flows either.

THE WINNING PATTERN:

    Don't beat reCAPTCHA — *borrow* a warmed identity. Read the user's live
    session cookies straight out of their REAL browser profile on disk and
    inject them into the automation context. Now you navigate as the already
    authenticated, already trusted user and skip the reCAPTCHA-gated login
    entirely. The reputation comes along for free.

This module locates real browser profiles, extracts their cookies, and
injects them into a Playwright/Camoufox BrowserContext.

Engine note: Wraith's primary engine is Camoufox (Firefox-engine stealth),
because reCAPTCHA-v3 / Reblaze ac_v2 take their Chrome-specific detection
branch only when isChrome() is true — a Firefox engine sidesteps that whole
cluster. So Firefox/Zen profiles are the most natural identity sources here:
same engine family, fully readable cookie store.

CAVEAT — not everything is a cookie. Many auth bearer tokens are minted
per-session and sent as an `Authorization` header, never stored as a cookie.
Cookie borrowing recovers the *session* cookie; for those bearer tokens you
also need live harvesting from network requests (see the harvest module).
"""

from __future__ import annotations

import platform
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "Cookie",
    "find_firefox_profiles",
    "find_zen_profiles",
    "find_chrome_profile",
    "extract_cookies",
    "to_playwright_cookies",
    "inject_cookies",
    "ChromeEncryptionError",
]


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Cookie:
    """One borrowed cookie, normalized to Playwright's vocabulary.

    ``same_site`` is one of "None" / "Lax" / "Strict" (Playwright's casing).
    A "None" cookie is only valid when ``secure`` is True, which we enforce
    in :func:`to_playwright_cookies`.
    """

    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = False
    http_only: bool = False
    same_site: str = "Lax"
    expires: float | None = None
    source: str = "unknown"  # profile path / browser this came from


class ChromeEncryptionError(NotImplementedError):
    """Raised when Chrome/Chromium cookies are OS-keychain (AES) encrypted.

    Decryption is an advanced extra and intentionally not implemented in the
    base toolkit (it requires unlocking the OS keychain). See the message for
    guidance on what to do instead.
    """


# --------------------------------------------------------------------------- #
# Firefox sameSite mapping
# --------------------------------------------------------------------------- #
# Firefox stores sameSite as a small integer in moz_cookies.sameSite:
#   0 -> None     1 -> Lax     2 -> Strict
# Some Firefox builds also write transient/unknown values; anything we don't
# recognise is treated as unset and falls back to "Lax" (the browser default),
# which is the conservative choice — it never silently upgrades a cookie to the
# secure-requiring "None" mode.
_FF_SAMESITE = {0: "None", 1: "Lax", 2: "Strict"}


def _map_firefox_samesite(raw: Any) -> str:
    try:
        return _FF_SAMESITE.get(int(raw), "Lax")
    except (TypeError, ValueError):
        return "Lax"


# Plausible cookie-expiry epoch in *seconds* lives in roughly [1e9, 1e11)
# (year 2001 .. year 5138). Firefox's moz_cookies.expiry unit is not stable
# across builds: classic Firefox wrote seconds, but recent Firefox/Zen builds
# write milliseconds (and other time columns are microseconds). Playwright's
# add_cookies expects SECONDS, so normalize by magnitude rather than trusting
# a fixed unit. Values that look like ms (1e12..) or µs (1e15..) get scaled.
_EPOCH_SECONDS_MAX = 1e11  # ~ year 5138; anything bigger is sub-second units


def _normalize_expiry_seconds(raw: Any) -> float | None:
    """Coerce a Firefox expiry timestamp to epoch *seconds*, or None.

    expiry <= 0 means a session cookie (no persistent expiry) -> None.
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    # Scale down ms / µs / (defensive) ns until it lands in seconds range.
    while v >= _EPOCH_SECONDS_MAX:
        v /= 1000.0
    return v


# --------------------------------------------------------------------------- #
# Profile discovery
# --------------------------------------------------------------------------- #

def _app_support_roots() -> list[Path]:
    """Per-OS roots under which browser support dirs live."""
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        return [home / "Library" / "Application Support"]
    if system == "Windows":
        import os

        roots: list[Path] = []
        for var in ("APPDATA", "LOCALAPPDATA"):
            v = os.environ.get(var)
            if v:
                roots.append(Path(v))
        return roots or [home / "AppData" / "Roaming"]
    # Linux / other
    return [
        home / ".mozilla",
        home / ".config",
        home / ".var" / "app",  # flatpak
    ]


def _identity_key(path: Path) -> Any:
    """A dedupe key that survives case-insensitive filesystems.

    On a case-insensitive APFS volume (macOS default) ``Path.resolve()``
    preserves the typed case, so "zen" and "Zen" resolve to *different*
    strings while pointing at the same directory. Stat (st_dev, st_ino)
    identifies the real on-disk directory regardless of casing; fall back to
    the resolved string if stat fails.
    """
    try:
        st = path.stat()
        return (st.st_dev, st.st_ino)
    except OSError:
        try:
            return str(path.resolve())
        except OSError:
            return str(path)


def _mozilla_profile_dirs(app_dirs: Iterable[Path]) -> list[Path]:
    """Return profile directories (those containing cookies.sqlite) under the
    given Mozilla-style application directories (Firefox or Zen)."""
    found: list[Path] = []
    seen: set[Any] = set()
    for app_dir in app_dirs:
        profiles_root = app_dir / "Profiles"
        candidates: list[Path] = []
        if profiles_root.is_dir():
            candidates.extend(p for p in profiles_root.iterdir() if p.is_dir())
        # Some Linux layouts put profiles directly under the app dir.
        if app_dir.is_dir():
            candidates.extend(p for p in app_dir.iterdir() if p.is_dir())
        for prof in candidates:
            if not (prof / "cookies.sqlite").is_file():
                continue
            key = _identity_key(prof)
            if key in seen:
                continue
            seen.add(key)
            found.append(prof)
    return found


def find_firefox_profiles() -> list[Path]:
    """Locate Firefox profile directories that contain a cookie store.

    Returns a list of paths (a user can have several profiles); each path is
    a directory containing ``cookies.sqlite``.
    """
    app_dirs: list[Path] = []
    for root in _app_support_roots():
        app_dirs.append(root / "Firefox")
        app_dirs.append(root / "Mozilla" / "Firefox")  # Windows / some Linux
        app_dirs.append(root / "firefox")  # flatpak: ~/.var/app/.../firefox
        app_dirs.append(root / "org.mozilla.firefox" / "firefox")  # flatpak
    return _mozilla_profile_dirs(app_dirs)


def find_zen_profiles() -> list[Path]:
    """Locate Zen Browser profile directories that contain a cookie store.

    Zen is a Firefox fork and uses the identical ``Profiles/*/cookies.sqlite``
    layout, so it is a first-class identity source for the Camoufox (Firefox)
    engine.
    """
    app_dirs: list[Path] = []
    for root in _app_support_roots():
        app_dirs.append(root / "zen")
        app_dirs.append(root / "Zen")
        app_dirs.append(root / "app.zen_browser.zen" / "zen")  # flatpak
    return _mozilla_profile_dirs(app_dirs)


def find_chrome_profile(profile: str = "Default") -> Path | None:
    """Locate a Chrome/Chromium profile directory containing a ``Cookies`` DB.

    Returns the profile directory (e.g. ``.../Google/Chrome/Default``) or None.
    Note: Chrome cookie *values* are AES-encrypted via the OS keychain on
    macOS/Windows; :func:`extract_cookies` will raise :class:`ChromeEncryptionError`
    with guidance rather than return garbage.
    """
    rel_candidates = [
        ("Google", "Chrome"),
        ("Google", "Chrome Beta"),
        ("Chromium",),
        ("BraveSoftware", "Brave-Browser"),
        ("Microsoft", "Edge"),
    ]
    for root in _app_support_roots():
        for rel in rel_candidates:
            base = root.joinpath(*rel)
            prof_dir = base / profile
            if (prof_dir / "Cookies").is_file():
                return prof_dir
            # Linux Chrome keeps Cookies under Default/ too but base may differ
    return None


# --------------------------------------------------------------------------- #
# Cookie extraction
# --------------------------------------------------------------------------- #

def _domain_matches(host: str, domain_filter: str | None) -> bool:
    if not domain_filter:
        return True
    h = host.lstrip(".").lower()
    f = domain_filter.lstrip(".").lower()
    # match the domain itself and any subdomain
    return h == f or h.endswith("." + f)


def _is_chrome_cookie_db(db_path: Path) -> bool:
    """A Chrome cookie DB has a ``cookies`` table; Firefox has ``moz_cookies``."""
    name = db_path.name.lower()
    return name == "cookies"


def _copy_db_to_temp(db_path: Path) -> tuple[Path, Path]:
    """Copy a (possibly live-locked) sqlite DB plus its -wal sidecar to a temp
    dir so we can read it without contending with the running browser.

    Firefox/Zen keep the cookie store in WAL mode and hold a lock while
    running; copying the .sqlite *and* the .sqlite-wal lets sqlite replay
    pending writes from the WAL and gives us the freshest cookies. We also
    copy -shm if present. Returns (temp_dir, temp_db_path).
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="wraith_id_"))
    tmp_db = tmp_dir / db_path.name
    shutil.copy2(db_path, tmp_db)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, tmp_dir / (db_path.name + suffix))
    return tmp_dir, tmp_db


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a copied sqlite file read-only (immutable=0 so WAL is replayed)."""
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def extract_cookies(
    profile_path: str | Path,
    domain_filter: str | None = None,
) -> list[Cookie]:
    """Extract cookies from a real browser profile directory.

    ``profile_path`` is a directory returned by one of the ``find_*`` helpers,
    or a direct path to a ``cookies.sqlite`` / ``Cookies`` file.

    ``domain_filter`` (e.g. ``"elal.com"``) keeps only cookies for that domain
    and its subdomains; pass None to take everything.

    Firefox/Zen profiles are read from ``moz_cookies``. The DB is copied to a
    temp location (with its -wal sidecar) before reading, because the browser
    holds a lock on the live file. Chrome/Chromium profiles raise
    :class:`ChromeEncryptionError` — their values are AES-encrypted via the OS
    keychain and base decryption is an advanced extra.
    """
    profile_path = Path(profile_path)

    if profile_path.is_dir():
        ff_db = profile_path / "cookies.sqlite"
        chrome_db = profile_path / "Cookies"
        if ff_db.is_file():
            db_path = ff_db
        elif chrome_db.is_file():
            db_path = chrome_db
        else:
            raise FileNotFoundError(
                f"No cookie store (cookies.sqlite or Cookies) found in {profile_path}"
            )
    elif profile_path.is_file():
        db_path = profile_path
    else:
        raise FileNotFoundError(f"Profile path does not exist: {profile_path}")

    if _is_chrome_cookie_db(db_path):
        _raise_chrome_encryption(db_path)

    return _extract_firefox(db_path, domain_filter)


def _extract_firefox(db_path: Path, domain_filter: str | None) -> list[Cookie]:
    tmp_dir, tmp_db = _copy_db_to_temp(db_path)
    cookies: list[Cookie] = []
    try:
        conn = _open_readonly(tmp_db)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT name, value, host, path, isSecure, isHttpOnly, "
                "sameSite, expiry FROM moz_cookies"
            ).fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    for r in rows:
        host = r["host"] or ""
        if not _domain_matches(host, domain_filter):
            continue
        cookies.append(
            Cookie(
                name=r["name"] or "",
                value=r["value"] or "",
                domain=host,  # leading-dot domains preserved as-is
                path=r["path"] or "/",
                secure=bool(r["isSecure"]),
                http_only=bool(r["isHttpOnly"]),
                same_site=_map_firefox_samesite(r["sameSite"]),
                expires=_normalize_expiry_seconds(r["expiry"]),
                source=str(db_path),
            )
        )
    return cookies


def _raise_chrome_encryption(db_path: Path) -> None:
    system = platform.system()
    keystore = {
        "Darwin": "the macOS Keychain (service 'Chrome Safe Storage')",
        "Windows": "DPAPI / the Windows Credential store",
        "Linux": "the Secret Service / kwallet (or a hardcoded 'peanuts' key)",
    }.get(system, "the OS credential store")
    raise ChromeEncryptionError(
        "Chrome/Chromium cookie values are AES-encrypted at rest "
        f"(prefix 'v10'/'v11'/'v20') with a key sealed in {keystore}; this "
        f"store ({db_path}) cannot be read as plaintext.\n\n"
        "This is an ADVANCED extra and is intentionally not implemented in "
        "the base toolkit. To borrow a Chrome identity you can instead:\n"
        "  1. Use a Firefox or Zen profile (find_firefox_profiles / "
        "find_zen_profiles) — Wraith's Camoufox engine is Firefox-family "
        "anyway, so this is the recommended path and needs no decryption.\n"
        "  2. Harvest the live session from network traffic instead of disk "
        "(see Wraith's harvest module): capture {Authorization, Cookie, "
        "User-Agent} from the first authenticated request.\n"
        "  3. Implement v10 decryption yourself: read the AES key from the OS "
        "keychain (PBKDF2 of 'Chrome Safe Storage' on macOS), then AES-128-CBC "
        "decrypt encrypted_value[3:]. Note Chrome 127+ adds app-bound 'v20' "
        "encryption which is materially harder."
    )


# --------------------------------------------------------------------------- #
# Playwright bridge
# --------------------------------------------------------------------------- #

def to_playwright_cookies(rows: Iterable[Cookie]) -> list[dict[str, Any]]:
    """Convert :class:`Cookie` objects into Playwright ``add_cookies`` dicts.

    Enforces the SameSite=None -> Secure invariant (Playwright/Chromium reject
    a non-secure "None" cookie). Drops cookies with no name. Leading-dot
    domains are kept verbatim, which is exactly what Playwright wants for a
    domain+path scoped cookie (vs. a host-only url-scoped one).
    """
    out: list[dict[str, Any]] = []
    for c in rows:
        if not c.name:
            continue
        same_site = c.same_site if c.same_site in ("None", "Lax", "Strict") else "Lax"
        secure = bool(c.secure)
        if same_site == "None":
            secure = True  # SameSite=None is invalid without Secure
        cookie: dict[str, Any] = {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path or "/",
            "secure": secure,
            "httpOnly": bool(c.http_only),
            "sameSite": same_site,
        }
        if c.expires is not None:
            # Playwright wants epoch seconds; -1 / 0 mean "session cookie".
            cookie["expires"] = c.expires
        out.append(cookie)
    return out


def inject_cookies(
    context: Any,
    cookies: Iterable[Cookie] | Iterable[dict[str, Any]],
) -> int:
    """Inject borrowed cookies into a Playwright/Camoufox BrowserContext.

    Accepts either :class:`Cookie` objects or already-converted Playwright
    cookie dicts. Calls ``context.add_cookies(...)`` and returns the number of
    cookies injected. After this the context navigates as the borrowed user.

    ``context`` is duck-typed (anything with ``add_cookies``) so this module
    imports cleanly without Playwright/Camoufox installed.
    """
    cookies = list(cookies)
    if cookies and isinstance(cookies[0], Cookie):
        payload = to_playwright_cookies(cookies)  # type: ignore[arg-type]
    else:
        payload = list(cookies)  # type: ignore[assignment]
    if not payload:
        return 0
    context.add_cookies(payload)
    return len(payload)
