"""Partitioned-cookie (CHIPS) metadata: extract, parse, and inject.

Firefox stores the key as URL-encoded ``(scheme, domain)`` in
``originAttributes``; Chrome stores a top-level site in ``top_frame_site_key``.
Both normalize to Playwright's ``partitionKey`` string.
"""

from __future__ import annotations

import sqlite3

from wraith import chrome
from wraith.identity import Cookie, _firefox_partition_key, to_playwright_cookies


def test_firefox_partition_key_decodes_to_a_site_url():
    assert (
        _firefox_partition_key("^partitionKey=%28https%2Cabb.com%29")
        == "https://abb.com"
    )


def test_firefox_partition_key_absent_or_unreadable_is_none():
    assert _firefox_partition_key("") is None
    assert _firefox_partition_key(None) is None
    assert _firefox_partition_key("firstPartyDomain=x") is None
    assert _firefox_partition_key("^partitionKey=not-a-pair") is None


def test_to_playwright_cookies_emits_partition_key():
    cookie = Cookie(
        name="sid",
        value="v",
        domain=".abb.com",
        partition_key="https://abb.com",
    )
    (out,) = to_playwright_cookies([cookie])
    assert out["partitionKey"] == "https://abb.com"


def test_to_playwright_cookies_omits_absent_partition_key():
    (out,) = to_playwright_cookies([Cookie(name="sid", value="v", domain=".x.com")])
    assert "partitionKey" not in out


def _make_db(path, partitioned):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, top_frame_site_key TEXT, name TEXT, "
        "value TEXT, encrypted_value BLOB, path TEXT, is_secure INT, "
        "is_httponly INT, samesite INT, expires_utc INT)"
    )
    conn.execute(
        "INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?,?)",
        (".abb.com", partitioned, "sid", "", b"v10raw", "/", 1, 0, 0, 0),
    )
    conn.commit()
    conn.close()


def test_chrome_extraction_reads_the_partition_key(tmp_path, monkeypatch):
    profile = tmp_path / "User Data" / "Default"
    _make_db(profile / "Cookies", "https://abb.com")
    monkeypatch.setattr(chrome, "get_chrome_key", lambda *a, **k: b"\x00" * 16)
    monkeypatch.setattr(chrome, "decrypt_chrome_value", lambda *a, **k: "decoded")

    (out,) = chrome.extract_chrome_cookies(profile)
    assert out.partition_key == "https://abb.com"


def test_chrome_extraction_without_the_column_still_works(tmp_path, monkeypatch):
    """An older Chrome has no ``top_frame_site_key``; the query must adapt."""
    profile = tmp_path / "User Data" / "Default"
    path = profile / "Cookies"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, path TEXT, is_secure INT, is_httponly INT, "
        "samesite INT, expires_utc INT)"
    )
    conn.execute(
        "INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?)",
        (".abb.com", "sid", "", b"v10raw", "/", 1, 0, 0, 0),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(chrome, "get_chrome_key", lambda *a, **k: b"\x00" * 16)
    monkeypatch.setattr(chrome, "decrypt_chrome_value", lambda *a, **k: "decoded")

    (out,) = chrome.extract_chrome_cookies(profile)
    assert out.partition_key is None
