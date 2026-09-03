"""Offline tests for wraith.providers.AnyIP — the anyIP residential/mobile provider.

No network: pure username-flag / URL construction + credential resolution.
Verifies the exact anyIP flag format (``user_<id>`` then ``,``-joined
``key_value`` flags), case rules (uppercase country, lowercase slugs), the
city→region→country dependency chain, sticky-session flags and bounds, the
sticky-session ProxyPool, credential resolution (explicit > env > secrets-file),
lazy auth errors, dashboard-username rejection, and that the repr never leaks
the password.
"""

from __future__ import annotations

import pytest

from wraith.providers import AnyIP, AnyIPAuthError
from wraith.proxy import ProxyPool, to_playwright_proxy

GW = "portal.anyip.io:1080"


# --------------------------------------------------------------------------- #
# rotating — no session flag (IP rotates per request)
# --------------------------------------------------------------------------- #

def test_rotating_bare_is_user_id_only():
    ai = AnyIP("user_ab12", "pw")
    assert ai.rotating() == f"http://user_ab12:pw@{GW}"


def test_username_gets_user_prefix_when_missing():
    assert AnyIP("ab12", "pw").username == "user_ab12"
    assert AnyIP("user_ab12", "pw").username == "user_ab12"
    assert AnyIP("  ab12  ", "pw").username == "user_ab12"


def test_flagged_dashboard_username_is_rejected():
    with pytest.raises(ValueError, match="bare account id"):
        AnyIP("user_ab12,type_residential,country_CA", "pw")
    with pytest.raises(ValueError):
        AnyIP("user_ab12|country_CA", "pw")


def test_rotating_country_is_uppercased():
    ai = AnyIP("ab12", "pw", country="il")
    assert ai.rotating() == f"http://user_ab12,country_IL:pw@{GW}"
    assert ",country_US" in ai.rotating(country="us")


def test_invalid_country_raises():
    with pytest.raises(ValueError):
        AnyIP("ab12", "pw", country="isr")
    with pytest.raises(ValueError):
        AnyIP("ab12", "pw").rotating(country="1l")


def test_network_flag_and_default_mixed():
    assert AnyIP("ab12", "pw").rotating() == f"http://user_ab12:pw@{GW}"  # mixed: no flag
    assert AnyIP("ab12", "pw", network="mobile").rotating() == f"http://user_ab12,type_mobile:pw@{GW}"
    assert ",type_residential" in AnyIP("ab12", "pw", network="Residential").rotating()


def test_unknown_network_raises():
    with pytest.raises(ValueError):
        AnyIP("ab12", "pw", network="datacenter")


def test_flag_order_is_stable():
    ai = AnyIP("ab12", "pw", network="mobile", country="us", region="Texas", city="Dallas", asn="AS7922")
    assert ai.rotating() == (
        f"http://user_ab12,type_mobile,country_US,region_texas,city_dallas,asn_7922:pw@{GW}"
    )


def test_slugs_lowercase_and_strip_whitespace():
    ai = AnyIP("ab12", "pw", country="US", region="New York", city="New York")
    assert ",region_newyork,city_newyork" in ai.rotating()


def test_pool_name_flag():
    ai = AnyIP("ab12", "pw", pool_name="Europe")
    assert ai.rotating() == f"http://user_ab12,pool_europe:pw@{GW}"


def test_asn_accepts_int_str_and_AS_prefix():
    assert ",asn_7922" in AnyIP("ab12", "pw", asn=7922).rotating()
    assert ",asn_7922" in AnyIP("ab12", "pw", asn="7922").rotating()
    assert ",asn_7922" in AnyIP("ab12", "pw", asn="as7922").rotating()
    with pytest.raises(ValueError):
        AnyIP("ab12", "pw", asn="seven")


def test_per_call_override_and_explicit_unset():
    ai = AnyIP("ab12", "pw", country="il", network="mobile")
    assert ",country_US" in ai.rotating(country="us")
    # "" explicitly unsets for this call
    assert ai.rotating(country="", network="") == f"http://user_ab12:pw@{GW}"


# --------------------------------------------------------------------------- #
# dependency chain: city -> region -> country
# --------------------------------------------------------------------------- #

def test_region_requires_country():
    with pytest.raises(ValueError, match="requires a country"):
        AnyIP("ab12", "pw", region="texas").rotating()


def test_city_requires_region():
    with pytest.raises(ValueError, match="requires a region"):
        AnyIP("ab12", "pw", country="us", city="dallas").rotating()


def test_dependency_checked_after_per_call_unset():
    ai = AnyIP("ab12", "pw", country="us", region="texas", city="dallas")
    assert ai.rotating().endswith(f"city_dallas:pw@{GW}")
    with pytest.raises(ValueError):
        ai.rotating(region="")  # city left dangling


# --------------------------------------------------------------------------- #
# sticky — session_ (+ sesstime_ / sessreplace_false / sessasn_strict)
# --------------------------------------------------------------------------- #

def test_sticky_has_session_flag():
    ai = AnyIP("ab12", "pw", country="il")
    assert ai.sticky("profile01") == f"http://user_ab12,country_IL,session_profile01:pw@{GW}"


def test_sticky_options():
    ai = AnyIP("ab12", "pw")
    url = ai.sticky("bank", minutes=30, replace=False, same_asn=True)
    assert url == f"http://user_ab12,session_bank,sesstime_30,sessreplace_false,sessasn_strict:pw@{GW}"


def test_sticky_session_id_validation():
    ai = AnyIP("ab12", "pw")
    for bad in ("", "has space", "dash-ed", "x" * 33, None):
        with pytest.raises(ValueError):
            ai.sticky(bad)  # type: ignore[arg-type]
    assert ",session_ig_client1" in ai.sticky("ig_client1")
    assert ",session_" + "a" * 32 in ai.sticky("a" * 32)


def test_sticky_minutes_bounds():
    ai = AnyIP("ab12", "pw")
    assert ",sesstime_1:" in ai.sticky("s", minutes=1)
    assert ",sesstime_10080:" in ai.sticky("s", minutes=10080)
    for bad in (0, 10081, -5):
        with pytest.raises(ValueError):
            ai.sticky("s", minutes=bad)


def test_sticky_per_call_targeting():
    ai = AnyIP("ab12", "pw")
    assert ai.sticky("s", network="mobile", country="il") == (
        f"http://user_ab12,type_mobile,country_IL,session_s:pw@{GW}"
    )


# --------------------------------------------------------------------------- #
# protocol / host / port
# --------------------------------------------------------------------------- #

def test_protocols_share_the_port():
    assert AnyIP("ab12", "pw").rotating().startswith("http://")
    assert AnyIP("ab12", "pw", protocol="https").rotating() == f"https://user_ab12:pw@{GW}"
    assert AnyIP("ab12", "pw", protocol="socks5").rotating() == f"socks5://user_ab12:pw@{GW}"


def test_alt_port_and_regional_host():
    ai = AnyIP("ab12", "pw", host="portal-eu.anyip.io", port=443)
    assert ai.rotating() == "http://user_ab12:pw@portal-eu.anyip.io:443"


def test_unknown_protocol_raises():
    with pytest.raises(ValueError):
        AnyIP("ab12", "pw", protocol="ftp")


# --------------------------------------------------------------------------- #
# pool — n distinct sticky sessions
# --------------------------------------------------------------------------- #

def test_pool_returns_n_distinct_sticky_urls():
    ai = AnyIP("ab12", "pw", country="il")
    pool = ai.pool(4)
    assert isinstance(pool, ProxyPool)
    assert len(pool) == 4
    seen = {pool.next() for _ in range(8)}
    assert seen == {f"http://user_ab12,country_IL,session_wraith{i}:pw@{GW}" for i in range(4)}


def test_pool_default_size_is_five():
    assert len(AnyIP("ab12", "pw").pool()) == 5


def test_pool_forwards_minutes_same_asn_and_targeting():
    pool = AnyIP("ab12", "pw").pool(2, minutes=30, same_asn=True, network="mobile", country="us")
    urls = sorted(pool.next() for _ in range(2))
    assert urls == [
        f"http://user_ab12,type_mobile,country_US,session_wraith{i},sesstime_30,sessasn_strict:pw@{GW}"
        for i in range(2)
    ]


def test_pool_non_sticky_collapses_to_one_rotating_endpoint():
    pool = AnyIP("ab12", "pw", country="il").pool(5, sticky=False)
    assert len(pool) == 1
    assert pool.next() == f"http://user_ab12,country_IL:pw@{GW}"


def test_pool_size_must_be_positive():
    with pytest.raises(ValueError):
        AnyIP("ab12", "pw").pool(0)


def test_pool_forwards_strategy():
    assert AnyIP("ab12", "pw").pool(3, strategy="random").strategy == "random"


# --------------------------------------------------------------------------- #
# the emitted string survives the trip to Playwright's dict form
# --------------------------------------------------------------------------- #

def test_url_round_trips_through_to_playwright_proxy():
    url = AnyIP("ab12", "pw", network="mobile", country="il").sticky("s1", minutes=30)
    assert to_playwright_proxy(url) == {
        "server": f"http://{GW}",
        "username": "user_ab12,type_mobile,country_IL,session_s1,sesstime_30",
        "password": "pw",
    }


# --------------------------------------------------------------------------- #
# credential resolution
# --------------------------------------------------------------------------- #

def test_creds_from_env(monkeypatch):
    monkeypatch.setenv("ANYIP_USERNAME", "envid")
    monkeypatch.setenv("ANYIP_PASSWORD", "envpass")
    ai = AnyIP()
    assert ai.username == "user_envid"
    assert ai.password == "envpass"
    assert ai.rotating() == f"http://user_envid:envpass@{GW}"


def test_explicit_args_beat_env(monkeypatch):
    monkeypatch.setenv("ANYIP_USERNAME", "envid")
    monkeypatch.setenv("ANYIP_PASSWORD", "envpass")
    ai = AnyIP("user_explicit", "xpw")
    assert ai.username == "user_explicit"
    assert ai.password == "xpw"


def test_creds_from_secrets_file(monkeypatch, tmp_path):
    monkeypatch.delenv("ANYIP_USERNAME", raising=False)
    monkeypatch.delenv("ANYIP_PASSWORD", raising=False)
    secrets = tmp_path / "secrets"
    secrets.write_text(
        "# a comment\n"
        'export ANYIP_USERNAME="user_fileid"\n'
        "ANYIP_PASSWORD='filepass'\n"
        "DATAIMPULSE_USERNAME=ignore\n"
    )
    ai = AnyIP(secrets_file=str(secrets))
    assert ai.username == "user_fileid"
    assert ai.password == "filepass"


def test_secrets_file_skipped_when_from_env_false(monkeypatch, tmp_path):
    monkeypatch.delenv("ANYIP_USERNAME", raising=False)
    monkeypatch.delenv("ANYIP_PASSWORD", raising=False)
    secrets = tmp_path / "secrets"
    secrets.write_text("ANYIP_USERNAME=fileid\nANYIP_PASSWORD=filepass\n")
    ai = AnyIP(from_env=False, secrets_file=str(secrets))
    assert ai.username is None
    assert ai.password is None


def test_dataimpulse_env_does_not_leak_into_anyip(monkeypatch):
    monkeypatch.delenv("ANYIP_USERNAME", raising=False)
    monkeypatch.delenv("ANYIP_PASSWORD", raising=False)
    monkeypatch.setenv("DATAIMPULSE_USERNAME", "di")
    monkeypatch.setenv("DATAIMPULSE_PASSWORD", "dipw")
    ai = AnyIP(secrets_file="/nonexistent/.secrets-xyz")
    assert ai.username is None and ai.password is None


# --------------------------------------------------------------------------- #
# missing creds -> lazy AnyIPAuthError
# --------------------------------------------------------------------------- #

def test_missing_creds_construction_does_not_raise(monkeypatch):
    monkeypatch.delenv("ANYIP_USERNAME", raising=False)
    monkeypatch.delenv("ANYIP_PASSWORD", raising=False)
    ai = AnyIP(secrets_file="/nonexistent/.secrets-xyz")
    assert ai.username is None and ai.password is None


def test_missing_creds_raises_on_first_url_request(monkeypatch):
    monkeypatch.delenv("ANYIP_USERNAME", raising=False)
    monkeypatch.delenv("ANYIP_PASSWORD", raising=False)
    ai = AnyIP(secrets_file="/nonexistent/.secrets-xyz")
    for call in (ai.rotating, lambda: ai.sticky("x"), lambda: ai.pool(3)):
        with pytest.raises(AnyIPAuthError, match="ANYIP_USERNAME"):
            call()


def test_missing_password_only_raises(monkeypatch):
    monkeypatch.setenv("ANYIP_USERNAME", "u")
    monkeypatch.delenv("ANYIP_PASSWORD", raising=False)
    ai = AnyIP(secrets_file="/nonexistent/.secrets-xyz")
    with pytest.raises(AnyIPAuthError, match="ANYIP_PASSWORD"):
        ai.rotating()


# --------------------------------------------------------------------------- #
# repr never leaks the password
# --------------------------------------------------------------------------- #

def test_repr_hides_password():
    r = repr(AnyIP("ab12", "supersecret", country="il"))
    assert "supersecret" not in r
    assert "user_ab12" in r and "***" in r and "'IL'" in r


def test_repr_unset_creds():
    r = repr(AnyIP(from_env=False))
    assert "<unset>" in r


# --------------------------------------------------------------------------- #
# package re-export
# --------------------------------------------------------------------------- #

def test_reexported_from_package():
    import wraith

    assert "AnyIP" in wraith.__all__
    assert "AnyIPAuthError" in wraith.__all__
    assert wraith.AnyIP is AnyIP
