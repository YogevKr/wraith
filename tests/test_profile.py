"""Profile-sync orchestration tests (no browser, no network).

Cover the jar build/summary/round-trip, the disk-source dispatch, and the
sync-then-receive path with the dead-drop stubbed to an in-memory relay.
"""

import pytest

from wraith import deaddrop, profile
from wraith.identity import Cookie


def _cookie(name, domain, value="v"):
    return Cookie(name=name, value=value, domain=domain, secure=True, same_site="Lax")


def test_jar_from_cookies_shape():
    jar = profile.jar_from_cookies([_cookie("sid", ".elal.com")])
    assert jar["origins"] == []
    assert jar["cookies"][0]["name"] == "sid"
    assert jar["cookies"][0]["domain"] == ".elal.com"


def test_jar_summary_counts_by_domain():
    jar = profile.jar_from_cookies(
        [_cookie("a", "x.com"), _cookie("b", "x.com"), _cookie("c", "y.com")]
    )
    assert profile.jar_summary(jar) == {"x.com": 2, "y.com": 1}


def test_jar_summary_has_no_values():
    # A summary must expose domains and counts only, never cookie values.
    jar = profile.jar_from_cookies([_cookie("sid", "bank.com", value="SECRET")])
    assert "SECRET" not in repr(profile.jar_summary(jar))


def test_jar_bytes_roundtrip():
    jar = profile.jar_from_cookies([_cookie("sid", "x.com")])
    assert profile.jar_from_bytes(profile.jar_to_bytes(jar)) == jar


def test_gather_cookies_rejects_unknown_source():
    with pytest.raises(ValueError, match="unknown disk source"):
        profile.gather_cookies("safari", "x.com")


def test_gather_cookies_dispatches_chrome(monkeypatch):
    called = {}

    def fake_extract(path, domain, browser=None):
        called["args"] = (path, domain, browser)
        return [_cookie("sid", "x.com")]

    import wraith.chrome as chrome_mod

    monkeypatch.setattr(chrome_mod, "extract_chrome_cookies", fake_extract)
    monkeypatch.setattr(profile, "_find_profile", lambda kind: f"/fake/{kind}")

    out = profile.gather_cookies("chrome", "x.com", browser="brave")
    assert out[0].name == "sid"
    assert called["args"] == ("/fake/chrome", "x.com", "brave")


def test_gather_cookies_dispatches_firefox(monkeypatch):
    from wraith import identity

    monkeypatch.setattr(profile, "_find_profile", lambda kind: f"/fake/{kind}")
    monkeypatch.setattr(
        identity, "extract_cookies", lambda path, domain_filter=None: [_cookie("ff", "x.com")]
    )
    out = profile.gather_cookies("firefox", "x.com")
    assert out[0].name == "ff"


def test_sync_then_receive_via_in_memory_relay(monkeypatch):
    # Stub the relay: push seals + stores; pull opens from the same store.
    store: dict[str, bytes] = {}

    def fake_push(relay_url, jar_bytes, *, secret=None, timeout=30.0):
        secret = secret or deaddrop.new_secret()
        slot, _ = deaddrop.derive(secret)
        store[slot] = deaddrop.seal(jar_bytes, secret)
        return deaddrop.format_code(relay_url, secret)

    def fake_pull(code, *, timeout=30.0, max_age=600):
        _relay_url, secret = deaddrop.parse_code(code)
        slot, _ = deaddrop.derive(secret)
        return deaddrop.open_sealed(store[slot], secret, max_age=max_age)

    monkeypatch.setattr(deaddrop, "push", fake_push)
    monkeypatch.setattr(deaddrop, "pull", fake_pull)
    monkeypatch.setattr(
        profile, "gather_cookies", lambda *a, **k: [_cookie("sid", "elal.com")]
    )

    code, summary = profile.sync_profile("https://relay.test", source="chrome", domain="elal.com")
    assert summary == {"elal.com": 1}
    jar = profile.receive_profile(code)
    assert jar["cookies"][0]["name"] == "sid"


def test_sync_raises_on_empty_jar(monkeypatch):
    monkeypatch.setattr(profile, "gather_cookies", lambda *a, **k: [])
    with pytest.raises(RuntimeError, match="no cookies found"):
        profile.sync_profile("https://relay.test", source="chrome", domain="elal.com")


def test_sync_login_needs_url():
    with pytest.raises(ValueError, match="needs login_url"):
        profile.sync_profile("https://relay.test", source="login", domain="x.com")
