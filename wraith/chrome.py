"""Chrome/Chromium cookie decryption — the encrypted-store identity source.

Firefox/Zen keep cookies in a plaintext ``value`` column, so
:mod:`wraith.identity` reads them directly. Chrome-family browsers encrypt each
cookie value at rest with an OS-sealed key, so :func:`wraith.identity.extract_cookies`
raises :class:`~wraith.identity.ChromeEncryptionError` by default. This module
is the opt-in decryptor for the ``profile sync`` flow: it recovers the OS key,
decrypts the values, and returns the same :class:`~wraith.identity.Cookie`
objects the rest of Wraith consumes.

Per-OS key + cipher (Chromium's ``os_crypt``):

* macOS  — key from the login Keychain item ``<Browser> Safe Storage``,
  PBKDF2-HMAC-SHA1(password, ``b"saltysalt"``, 1003 iters, 16 bytes).
  ``v10`` values: AES-128-CBC, IV = 16 spaces.
* Linux  — key from the Secret Service (``v11``) or the fixed password
  ``"peanuts"`` (``v10``), PBKDF2-HMAC-SHA1(..., 1 iter, 16 bytes).
  Same AES-128-CBC/spaces-IV cipher.
* Windows — AES-256-GCM key DPAPI-sealed in ``Local State`` ``os_crypt.encrypted_key``.
  ``v10``/``v11`` values: 12-byte nonce ‖ ciphertext ‖ 16-byte tag.
  ``v20`` values are app-bound (Chrome 127+) and need SYSTEM — unreadable from a
  user process; those raise with guidance to use ``--from login`` instead.

Chrome ~130+ prepends ``sha256(host_key)`` (32 bytes) to the *decrypted*
plaintext. We strip it deterministically: we know ``host_key``, so we only strip
when the plaintext actually starts with that hash.
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .identity import ChromeEncryptionError, Cookie, _domain_matches

__all__ = [
    "AppBoundCookieError",
    "ChromeCookieError",
    "decrypt_chrome_value",
    "extract_chrome_cookies",
    "get_chrome_key",
]


class ChromeCookieError(ChromeEncryptionError):
    """Chrome cookie decryption failed (missing key, app-bound value, etc.).

    Subclasses :class:`~wraith.identity.ChromeEncryptionError` so callers that
    already catch the base "this store is encrypted" error keep working.
    """


class AppBoundCookieError(ChromeCookieError):
    """A cookie uses app-bound ('v20') encryption, unreadable from user space.

    Raised for the Chrome 127+ (Windows) app-bound cipher. Carrying its own type
    lets the extractor tell "this store needs `--from login`" apart from a merely
    corrupt cookie, so it can surface the right guidance instead of a misleading
    empty result.
    """


# --------------------------------------------------------------------------- #
# Chromium sameSite mapping (integer -> Playwright casing)
# --------------------------------------------------------------------------- #
# Chromium stores samesite as: -1 unspecified, 0 None, 1 Lax, 2 Strict.
_CHROME_SAMESITE = {-1: "Lax", 0: "None", 1: "Lax", 2: "Strict"}


def _map_chrome_samesite(raw: Any) -> str:
    try:
        return _CHROME_SAMESITE.get(int(raw), "Lax")
    except (TypeError, ValueError):
        return "Lax"


# Chromium expiry is microseconds since 1601-01-01 (Windows FILETIME epoch).
# Convert to POSIX epoch seconds. 0 (or <=0) means a session cookie -> None.
_WINDOWS_TO_POSIX_EPOCH = 11644473600  # seconds between 1601-01-01 and 1970-01-01


def _chrome_expiry_seconds(raw: Any) -> float | None:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v / 1_000_000 - _WINDOWS_TO_POSIX_EPOCH


# --------------------------------------------------------------------------- #
# Per-OS key retrieval
# --------------------------------------------------------------------------- #
# Keychain / Secret-Service service label per Chromium-family browser.
_SAFE_STORAGE_LABEL = {
    "chrome": "Chrome",
    "chrome beta": "Chrome",
    "chromium": "Chromium",
    "brave": "Brave",
    "brave-browser": "Brave",
    "edge": "Microsoft Edge",
}


def _safe_storage_service(browser: str) -> str:
    """The ``<Name> Safe Storage`` service label for a browser."""
    key = (browser or "chrome").strip().lower()
    name = _SAFE_STORAGE_LABEL.get(key, "Chromium")
    return f"{name} Safe Storage"


def _pbkdf2_key(password: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", iterations, dklen=16)


def _macos_key(browser: str) -> bytes:
    service = _safe_storage_service(browser)
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ChromeCookieError(
            f"Could not run the macOS 'security' tool to read the {service!r} "
            f"Keychain item: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise ChromeCookieError(
            f"No {service!r} item in the login Keychain (security exited "
            f"{proc.returncode}). Open the browser once so it creates the key, "
            "and approve the Keychain prompt."
        )
    password = proc.stdout.strip().encode("utf-8")
    return _pbkdf2_key(password, iterations=1003)


def _linux_key(browser: str) -> bytes:
    service = _safe_storage_service(browser)
    password = b"peanuts"  # v10 fallback when no keyring is present
    tool = shutil.which("secret-tool")
    if tool:
        try:
            proc = subprocess.run(
                [tool, "lookup", "application", (browser or "chrome").lower()],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                password = proc.stdout.rstrip(b"\n") or password
        except (OSError, subprocess.SubprocessError):
            pass  # fall back to 'peanuts'
    _ = service  # label kept for symmetry / future keyring backends
    return _pbkdf2_key(password, iterations=1)


def _windows_key(local_state_path: Path) -> bytes:
    try:
        state = json.loads(local_state_path.read_text(encoding="utf-8"))
        b64 = state["os_crypt"]["encrypted_key"]
    except (OSError, KeyError, ValueError) as exc:
        raise ChromeCookieError(
            f"Could not read os_crypt.encrypted_key from {local_state_path}: {exc}"
        ) from exc
    blob = base64.b64decode(b64)
    if blob[:5] != b"DPAPI":
        raise ChromeCookieError("Local State key is not DPAPI-prefixed")
    try:
        import ctypes
        import ctypes.wintypes as wt

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        payload = blob[5:]
        blob_in = DATA_BLOB(len(payload), ctypes.create_string_buffer(payload, len(payload)))
        blob_out = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            raise ChromeCookieError("CryptUnprotectData failed on the DPAPI key")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except AttributeError as exc:  # pragma: no cover - non-Windows
        raise ChromeCookieError("DPAPI is only available on Windows") from exc


def get_chrome_key(profile_path: Path, browser: str) -> bytes:
    """Return the AES key that decrypts this profile's cookie values.

    ``profile_path`` is the profile directory (its parent holds ``Local State``
    on Windows). ``browser`` selects the Keychain/Secret-Service label.
    """
    system = platform.system()
    if system == "Darwin":
        return _macos_key(browser)
    if system == "Windows":
        # Local State sits beside the profile dir (…/User Data/Local State).
        return _windows_key(profile_path.parent / "Local State")
    return _linux_key(browser)


# --------------------------------------------------------------------------- #
# Value decryption
# --------------------------------------------------------------------------- #

def _strip_host_hash(plaintext: bytes, host_key: str) -> bytes:
    """Drop the Chrome 130+ ``sha256(host_key)`` prefix when present."""
    if len(plaintext) >= 32:
        digest = hashlib.sha256(host_key.encode("utf-8")).digest()
        if plaintext[:32] == digest:
            return plaintext[32:]
    return plaintext


def _unpad_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def decrypt_chrome_value(
    encrypted_value: bytes,
    key: bytes,
    host_key: str,
    *,
    plaintext_value: str = "",
) -> str:
    """Decrypt one Chromium ``encrypted_value`` blob to a cookie string.

    ``plaintext_value`` is the row's legacy ``value`` column; some rows are
    unencrypted and carry the value there. ``host_key`` drives the 130+
    host-hash strip. Raises :class:`ChromeCookieError` on an app-bound (``v20``)
    value or a cipher failure.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not encrypted_value:
        return plaintext_value
    prefix = encrypted_value[:3]

    if prefix == b"v20":
        raise AppBoundCookieError(
            "App-bound ('v20') cookie encryption (Chrome 127+ on Windows) cannot "
            "be read from a user process — it needs SYSTEM. Use `--from login` to "
            "capture the session with a manual sign-in instead."
        )

    if prefix in (b"v10", b"v11"):
        body = encrypted_value[3:]
        system = platform.system()
        if system == "Windows":
            nonce, ct, tag = body[:12], body[12:-16], body[-16:]
            aesgcm = Cipher(algorithms.AES(key), modes.GCM(nonce, tag))
            dec = aesgcm.decryptor()
            try:
                plaintext = dec.update(ct) + dec.finalize()
            except Exception as exc:  # invalid tag / wrong key
                raise ChromeCookieError(f"AES-GCM decrypt failed: {exc}") from exc
        else:
            iv = b" " * 16
            cbc = Cipher(algorithms.AES(key), modes.CBC(iv))
            dec = cbc.decryptor()
            padded = dec.update(body) + dec.finalize()
            plaintext = _unpad_pkcs7(padded)
        plaintext = _strip_host_hash(plaintext, host_key)
        return plaintext.decode("utf-8", errors="replace")

    # No known version prefix: an already-plaintext value (rare) or unknown.
    if plaintext_value:
        return plaintext_value
    try:
        return encrypted_value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChromeCookieError(
            f"Unknown cookie encryption prefix {prefix!r}; cannot decrypt"
        ) from exc


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

def _copy_db_to_temp(db_path: Path) -> tuple[Path, Path]:
    """Copy the (WAL-mode, live-locked) Cookies DB + sidecars to a temp dir."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="wraith_chrome_"))
    tmp_db = tmp_dir / db_path.name
    shutil.copy2(db_path, tmp_db)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, tmp_dir / (db_path.name + suffix))
    return tmp_dir, tmp_db


def _browser_name_from_path(profile_path: Path) -> str:
    """Guess the Keychain/Secret-Service label from the profile path."""
    lowered = str(profile_path).lower()
    for token, name in (
        ("brave", "brave"),
        ("chromium", "chromium"),
        ("edge", "edge"),
        ("chrome", "chrome"),
    ):
        if token in lowered:
            return name
    return "chrome"


def extract_chrome_cookies(
    profile_path: str | Path,
    domain_filter: str | None = None,
    *,
    browser: str | None = None,
) -> list[Cookie]:
    """Extract and DECRYPT cookies from a Chrome/Chromium profile directory.

    ``profile_path`` is a profile dir (e.g. ``…/Google/Chrome/Default``) or a
    direct ``Cookies`` DB path. ``domain_filter`` keeps only a domain and its
    subdomains. ``browser`` overrides the auto-detected Keychain label.

    Returns the same :class:`~wraith.identity.Cookie` objects the Firefox path
    yields, so :func:`wraith.identity.inject_cookies` /
    :func:`wraith.identity.to_playwright_cookies` consume them unchanged.
    """
    profile_path = Path(profile_path)
    if profile_path.is_dir():
        profile_dir = profile_path
        db_path = profile_dir / "Cookies"
        if not db_path.is_file():
            db_path = profile_dir / "Network" / "Cookies"  # Chrome 96+ layout
        if not db_path.is_file():
            raise FileNotFoundError(f"No Cookies DB under {profile_path}")
    elif profile_path.is_file():
        db_path = profile_path
        # The profile dir is the DB's parent, except under the Chrome 96+
        # ``…/<Profile>/Network/Cookies`` layout, where it is the grandparent.
        # get_chrome_key derives Local State from the profile dir's parent, so
        # this must be the real profile dir (…/User Data/Default), not Network/.
        profile_dir = (
            db_path.parent.parent if db_path.parent.name == "Network" else db_path.parent
        )
    else:
        raise FileNotFoundError(f"Profile path does not exist: {profile_path}")

    label = browser or _browser_name_from_path(profile_path)
    key = get_chrome_key(profile_dir, label)

    tmp_dir, tmp_db = _copy_db_to_temp(db_path)
    try:
        conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT host_key, name, value, encrypted_value, path, "
                "is_secure, is_httponly, samesite, expires_utc FROM cookies"
            ).fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    cookies: list[Cookie] = []
    app_bound_skipped = 0
    for r in rows:
        host = r["host_key"] or ""
        if not _domain_matches(host, domain_filter):
            continue
        try:
            value = decrypt_chrome_value(
                r["encrypted_value"] or b"",
                key,
                host,
                plaintext_value=r["value"] or "",
            )
        except AppBoundCookieError:
            # App-bound ('v20') values are unreadable from user space; remember
            # we hit one so we can surface the right guidance below.
            app_bound_skipped += 1
            continue
        except ChromeCookieError:
            # A single genuinely-corrupt value must not sink the whole export.
            continue
        cookies.append(
            Cookie(
                name=r["name"] or "",
                value=value,
                domain=host,
                path=r["path"] or "/",
                secure=bool(r["is_secure"]),
                http_only=bool(r["is_httponly"]),
                same_site=_map_chrome_samesite(r["samesite"]),
                expires=_chrome_expiry_seconds(r["expires_utc"]),
                source=str(db_path),
            )
        )

    # An all-app-bound store would otherwise return [] and read as "not signed
    # in". Surface the real cause and the fix instead of a misleading empty set.
    if not cookies and app_bound_skipped:
        raise AppBoundCookieError(
            f"All {app_bound_skipped} matching cookie(s) use app-bound ('v20') "
            "encryption (Chrome 127+ on Windows), unreadable from a user process. "
            "Use `--from login` to capture the session with a manual sign-in."
        )
    return cookies
