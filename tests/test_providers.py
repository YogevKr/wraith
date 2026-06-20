"""Offline tests for wraith.providers — the DataImpulse residential provider.

No network: pure URL/username construction + credential resolution. Verifies the
exact DataImpulse enrichment format (``__`` block, ``;``-joined ``key.value``
params), port/scheme selection, the sticky-session ProxyPool, credential
resolution (explicit > env > secrets-file), lazy auth errors, and that the repr
never leaks the password.
"""

from __future__ import annotations

import pytest

from wraith.providers import DataImpulse, DataImpulseAuthError
from wraith.proxy import ProxyPool


# --------------------------------------------------------------------------- #
# rotating — no sessid (IP rotates per request)
# --------------------------------------------------------------------------- #

def test_rotating_no_country_is_bare_username():
    di = DataImpulse("acct123", "pw")
    url = di.rotating()
    # No params -> bare base username, no "__" block.
    assert url == "http://acct123:pw@gw.dataimpulse.com:823"
    assert "__" not in url
    assert "sessid." not in url


def test_rotating_with_country():
    di = DataImpulse("acct123", "pw", country="il")
    url = di.rotating()
    assert url == "http://acct123__cr.il:pw@gw.dataimpulse.com:823"
    assert "__cr.il" in url
    assert "sessid." not in url  # rotating => no sticky session


def test_rotating_country_lowercased():
    di = DataImpulse("acct123", "pw")
    assert "__cr.us" in di.rotating(country="US")


def test_rotating_with_city():
    di = DataImpulse("acct123", "pw", country="us")
    url = di.rotating(city="newyork")
    assert url == "http://acct123__cr.us;city.newyork:pw@gw.dataimpulse.com:823"
    assert "city.newyork" in url


def test_per_call_country_overrides_default():
    di = DataImpulse("acct123", "pw", country="il")
    assert "__cr.us" in di.rotating(country="us")
    # explicit "" unsets the country for this call
    assert di.rotating(country="") == "http://acct123:pw@gw.dataimpulse.com:823"


# --------------------------------------------------------------------------- #
# sticky — has ;sessid.<id> (sticky IP)
# --------------------------------------------------------------------------- #

def test_sticky_has_sessid():
    di = DataImpulse("acct123", "pw", country="il")
    url = di.sticky("profile01")
    assert url == "http://acct123__cr.il;sessid.profile01:pw@gw.dataimpulse.com:823"
    assert ";sessid.profile01" in url


def test_sticky_no_country():
    di = DataImpulse("acct123", "pw")
    url = di.sticky("abc")
    # No country -> sessid is the first (and only) param, so "__sessid." not ";".
    assert url == "http://acct123__sessid.abc:pw@gw.dataimpulse.com:823"
    assert "cr." not in url
    assert "__sessid.abc" in url


def test_sticky_empty_session_id_raises():
    di = DataImpulse("acct123", "pw")
    with pytest.raises(ValueError):
        di.sticky("")


# --------------------------------------------------------------------------- #
# protocol / port selection
# --------------------------------------------------------------------------- #

def test_socks5_uses_port_824_and_scheme():
    di = DataImpulse("acct123", "pw", country="il", protocol="socks5")
    url = di.rotating()
    assert url.startswith("socks5://")
    assert url.endswith("gw.dataimpulse.com:824")
    assert di._port == 824


def test_http_and_https_use_port_823():
    assert DataImpulse("u", "p")._port == 823
    assert DataImpulse("u", "p", protocol="https")._port == 823
    assert DataImpulse("u", "p", protocol="https").rotating().startswith("https://")


def test_unknown_protocol_raises():
    with pytest.raises(ValueError):
        DataImpulse("u", "p", protocol="ftp")


# --------------------------------------------------------------------------- #
# pool — n distinct sticky sessions
# --------------------------------------------------------------------------- #

def test_pool_returns_proxypool_of_n_distinct_sticky_urls():
    di = DataImpulse("acct123", "pw", country="il")
    pool = di.pool(4)
    assert isinstance(pool, ProxyPool)
    assert len(pool) == 4
    # Drain it; every URL is a distinct sticky session wraith-0..wraith-3.
    seen = {pool.next() for _ in range(8)}
    assert len(seen) == 4
    assert seen == {
        f"http://acct123__cr.il;sessid.wraith-{i}:pw@gw.dataimpulse.com:823"
        for i in range(4)
    }


def test_pool_default_size_is_five():
    di = DataImpulse("acct123", "pw")
    assert len(di.pool()) == 5


def test_pool_non_sticky_collapses_to_one_rotating_endpoint():
    di = DataImpulse("acct123", "pw", country="il")
    pool = di.pool(5, sticky=False)
    # n identical rotating endpoints de-dupe down to one in ProxyPool.
    assert len(pool) == 1
    assert pool.next() == "http://acct123__cr.il:pw@gw.dataimpulse.com:823"


def test_pool_size_must_be_positive():
    di = DataImpulse("acct123", "pw")
    with pytest.raises(ValueError):
        di.pool(0)


def test_pool_forwards_strategy():
    di = DataImpulse("acct123", "pw")
    pool = di.pool(3, strategy="random")
    assert pool.strategy == "random"


# --------------------------------------------------------------------------- #
# credential resolution
# --------------------------------------------------------------------------- #

def test_creds_from_env(monkeypatch):
    monkeypatch.setenv("DATAIMPULSE_USERNAME", "envuser")
    monkeypatch.setenv("DATAIMPULSE_PASSWORD", "envpass")
    di = DataImpulse()  # no explicit args
    assert di.username == "envuser"
    assert di.password == "envpass"
    assert di.rotating() == "http://envuser:envpass@gw.dataimpulse.com:823"


def test_explicit_args_beat_env(monkeypatch):
    monkeypatch.setenv("DATAIMPULSE_USERNAME", "envuser")
    monkeypatch.setenv("DATAIMPULSE_PASSWORD", "envpass")
    di = DataImpulse("explicit", "xpw")
    assert di.username == "explicit"
    assert di.password == "xpw"


def test_creds_from_secrets_file(monkeypatch, tmp_path):
    monkeypatch.delenv("DATAIMPULSE_USERNAME", raising=False)
    monkeypatch.delenv("DATAIMPULSE_PASSWORD", raising=False)
    secrets = tmp_path / "secrets"
    secrets.write_text(
        "# a comment\n"
        'export DATAIMPULSE_USERNAME="fileuser"\n'
        "DATAIMPULSE_PASSWORD='filepass'\n"
        "UNRELATED=ignore\n"
    )
    di = DataImpulse(secrets_file=str(secrets))
    assert di.username == "fileuser"
    assert di.password == "filepass"
    assert di.rotating() == "http://fileuser:filepass@gw.dataimpulse.com:823"


def test_secrets_file_skipped_when_from_env_false(monkeypatch, tmp_path):
    monkeypatch.delenv("DATAIMPULSE_USERNAME", raising=False)
    monkeypatch.delenv("DATAIMPULSE_PASSWORD", raising=False)
    secrets = tmp_path / "secrets"
    secrets.write_text("DATAIMPULSE_USERNAME=fileuser\nDATAIMPULSE_PASSWORD=filepass\n")
    di = DataImpulse(from_env=False, secrets_file=str(secrets))
    assert di.username is None
    assert di.password is None


# --------------------------------------------------------------------------- #
# missing creds -> lazy DataImpulseAuthError
# --------------------------------------------------------------------------- #

def test_missing_creds_construction_does_not_raise(monkeypatch):
    monkeypatch.delenv("DATAIMPULSE_USERNAME", raising=False)
    monkeypatch.delenv("DATAIMPULSE_PASSWORD", raising=False)
    # Point at a path that won't exist so no secrets are picked up.
    di = DataImpulse(secrets_file="/nonexistent/.secrets-xyz")
    assert di.username is None
    assert di.password is None


def test_missing_creds_raises_on_first_url_request(monkeypatch):
    monkeypatch.delenv("DATAIMPULSE_USERNAME", raising=False)
    monkeypatch.delenv("DATAIMPULSE_PASSWORD", raising=False)
    di = DataImpulse(secrets_file="/nonexistent/.secrets-xyz")
    with pytest.raises(DataImpulseAuthError):
        di.rotating()
    with pytest.raises(DataImpulseAuthError):
        di.sticky("x")
    with pytest.raises(DataImpulseAuthError):
        di.pool(3)


def test_missing_password_only_raises(monkeypatch):
    monkeypatch.setenv("DATAIMPULSE_USERNAME", "u")
    monkeypatch.delenv("DATAIMPULSE_PASSWORD", raising=False)
    di = DataImpulse(secrets_file="/nonexistent/.secrets-xyz")
    with pytest.raises(DataImpulseAuthError):
        di.rotating()


# --------------------------------------------------------------------------- #
# repr never leaks the password
# --------------------------------------------------------------------------- #

def test_repr_hides_password():
    di = DataImpulse("acct123", "supersecret")
    r = repr(di)
    assert "supersecret" not in r
    assert "acct123" in r
    assert "***" in r


def test_repr_unset_creds():
    di = DataImpulse(from_env=False)
    r = repr(di)
    assert "supersecret" not in r
    assert "<unset>" in r


# --------------------------------------------------------------------------- #
# package re-export
# --------------------------------------------------------------------------- #

def test_reexported_from_package():
    import wraith

    assert "DataImpulse" in wraith.__all__
    assert "DataImpulseAuthError" in wraith.__all__
    assert wraith.DataImpulse is DataImpulse
