"""Chrome cookie decryption unit tests (no Keychain / no real profile).

These exercise the pure crypto and parsing so CI runs them on any OS: we build
``encrypted_value`` blobs with a known key and assert the decrypt path recovers
them, including the Chrome 130+ host-hash prefix and the v20 refusal.
"""

import hashlib
import sqlite3

import pytest

from wraith import chrome
from wraith.identity import ChromeEncryptionError


def _make_cookies_db(path, rows):
    """Write a minimal Chrome ``Cookies`` sqlite with the given rows.

    Each row: (host_key, name, encrypted_value bytes). value is left empty so
    decryption drives the result.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, path TEXT, is_secure INT, is_httponly INT, "
        "samesite INT, expires_utc INT)"
    )
    for host, name, enc in rows:
        conn.execute(
            "INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?)",
            (host, name, "", enc, "/", 1, 0, 0, 0),
        )
    conn.commit()
    conn.close()


def _aes_cbc_v10(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt like macOS/Linux Chromium: 'v10' + AES-128-CBC(spaces IV, PKCS7)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return b"v10" + enc.update(padded) + enc.finalize()


KEY = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, dklen=16)


def test_roundtrip_plain_value():
    host = "example.com"
    blob = _aes_cbc_v10(b"session=abc123", KEY)
    assert chrome.decrypt_chrome_value(blob, KEY, host) == "session=abc123"


def test_roundtrip_strips_chrome_130_host_hash():
    host = "www.elal.co.il"
    digest = hashlib.sha256(host.encode()).digest()
    blob = _aes_cbc_v10(digest + b"tokenXYZ", KEY)
    # The 32-byte host hash prefix must be stripped, leaving the real value.
    assert chrome.decrypt_chrome_value(blob, KEY, host) == "tokenXYZ"


def test_host_hash_not_stripped_for_other_host():
    # A value that merely happens to be >=32 bytes but is NOT prefixed by this
    # host's hash must survive intact.
    host = "example.com"
    blob = _aes_cbc_v10(b"x" * 40, KEY)
    assert chrome.decrypt_chrome_value(blob, KEY, host) == "x" * 40


def test_v20_is_refused_with_guidance():
    with pytest.raises(ChromeEncryptionError) as exc:
        chrome.decrypt_chrome_value(b"v20" + b"\x00" * 40, KEY, "example.com")
    assert "--from login" in str(exc.value)


def test_empty_value_returns_plaintext_column():
    assert chrome.decrypt_chrome_value(b"", KEY, "example.com", plaintext_value="raw") == "raw"


def test_samesite_mapping():
    assert chrome._map_chrome_samesite(-1) == "Lax"
    assert chrome._map_chrome_samesite(0) == "None"
    assert chrome._map_chrome_samesite(1) == "Lax"
    assert chrome._map_chrome_samesite(2) == "Strict"
    assert chrome._map_chrome_samesite("garbage") == "Lax"


def test_expiry_windows_epoch_to_posix():
    # 13361817600000000 µs since 1601 == 2024-06-02T00:00:00Z.
    secs = chrome._chrome_expiry_seconds(13_361_817_600_000_000)
    assert secs is not None
    # 2024-06-02 is ~1.7173e9 in POSIX seconds.
    assert 1.717e9 < secs < 1.718e9


def test_expiry_session_cookie_is_none():
    assert chrome._chrome_expiry_seconds(0) is None
    assert chrome._chrome_expiry_seconds(-5) is None


def test_safe_storage_service_labels():
    assert chrome._safe_storage_service("chrome") == "Chrome Safe Storage"
    assert chrome._safe_storage_service("brave") == "Brave Safe Storage"
    assert chrome._safe_storage_service("edge") == "Microsoft Edge Safe Storage"
    assert chrome._safe_storage_service("unknown-thing") == "Chromium Safe Storage"


def test_browser_name_from_path():
    assert chrome._browser_name_from_path("/x/Google/Chrome/Default") == "chrome"
    assert chrome._browser_name_from_path("/x/BraveSoftware/Brave-Browser/Default") == "brave"
    assert chrome._browser_name_from_path("/x/Chromium/Default") == "chromium"


def test_network_layout_resolves_profile_dir_for_local_state(tmp_path, monkeypatch):
    # Chrome 96+ keeps the DB at <Profile>/Network/Cookies. get_chrome_key must
    # receive the PROFILE dir (…/Default), not …/Default/Network, so the Windows
    # Local State lookup (profile_dir.parent) lands on …/User Data/Local State.
    profile = tmp_path / "User Data" / "Default"
    _make_cookies_db(profile / "Network" / "Cookies", [("x.com", "sid", b"v10raw")])
    seen = {}

    def fake_key(profile_dir, browser):
        seen["dir"] = profile_dir
        return b"\x00" * 16

    monkeypatch.setattr(chrome, "get_chrome_key", fake_key)
    monkeypatch.setattr(chrome, "decrypt_chrome_value", lambda *a, **k: "decoded")
    out = chrome.extract_chrome_cookies(profile)
    assert seen["dir"] == profile  # the Default dir, NOT Default/Network
    assert out[0].value == "decoded"


def test_all_app_bound_store_raises_guidance(tmp_path, monkeypatch):
    # An all-v20 store must surface the --from login guidance, not an empty list.
    profile = tmp_path / "User Data" / "Default"
    _make_cookies_db(
        profile / "Cookies",
        [("x.com", "a", b"v20" + b"\x00" * 40), ("x.com", "b", b"v20" + b"\x00" * 40)],
    )
    monkeypatch.setattr(chrome, "get_chrome_key", lambda *a, **k: b"\x00" * 16)
    with pytest.raises(chrome.AppBoundCookieError) as exc:
        chrome.extract_chrome_cookies(profile)
    assert "--from login" in str(exc.value)


def test_app_bound_is_a_chrome_encryption_error():
    # Back-compat: callers catching the base type still catch app-bound.
    assert issubclass(chrome.AppBoundCookieError, ChromeEncryptionError)


def test_find_chrome_profile_detects_both_cookie_layouts(tmp_path, monkeypatch):
    from wraith import identity

    monkeypatch.setattr(identity, "_app_support_roots", lambda: [tmp_path])

    # Modern layout: Default/Network/Cookies -> auto-detect must find it.
    net = tmp_path / "Google" / "Chrome" / "Default" / "Network"
    net.mkdir(parents=True)
    (net / "Cookies").write_bytes(b"x")
    assert identity.find_chrome_profile() == tmp_path / "Google" / "Chrome" / "Default"

    # Legacy layout: Default/Cookies directly.
    old = tmp_path / "Chromium" / "Default"
    old.mkdir(parents=True)
    (old / "Cookies").write_bytes(b"x")
    assert identity.find_chrome_profile() is not None
