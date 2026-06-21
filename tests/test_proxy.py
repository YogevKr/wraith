"""Offline tests for wraith.proxy — the dependency-free proxy pool.

No network, no Playwright: pure bookkeeping over proxy URL strings.
"""

from __future__ import annotations

import pytest

from wraith.proxy import ProxyPool, normalize_proxy


# --------------------------------------------------------------------------- #
# normalize_proxy
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("host:8080", "http://host:8080"),
        ("user:pass@host:8080", "http://user:pass@host:8080"),
        ("http://host:8080", "http://host:8080"),
        ("https://host:8080", "https://host:8080"),
        ("socks5://host:1080", "socks5://host:1080"),
        ("socks5h://host:1080", "socks5h://host:1080"),
        ("http://user:pass@host:8080", "http://user:pass@host:8080"),
        ("  host:8080  ", "http://host:8080"),  # whitespace stripped
    ],
)
def test_normalize_proxy_forms(raw, expected):
    assert normalize_proxy(raw) == expected


def test_normalize_proxy_explicit_scheme_passthrough():
    # Any explicit scheme:// is respected, not double-prefixed.
    assert normalize_proxy("custom://host:1") == "custom://host:1"


def test_normalize_proxy_empty_raises():
    with pytest.raises(ValueError):
        normalize_proxy("   ")


def test_normalize_proxy_non_str_raises():
    with pytest.raises(TypeError):
        normalize_proxy(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# ProxyPool — round_robin cycling
# --------------------------------------------------------------------------- #

def test_round_robin_cycles():
    pool = ProxyPool(["a:1", "b:2"])
    assert pool.next() == "http://a:1"
    assert pool.next() == "http://b:2"
    assert pool.next() == "http://a:1"  # wraps around
    assert pool.next() == "http://b:2"


def test_constructor_normalizes_and_dedupes():
    pool = ProxyPool(["a:1", "http://a:1", "b:2"])
    # "a:1" and "http://a:1" collapse to one entry.
    assert len(pool) == 2
    seen = {pool.next() for _ in range(4)}
    assert seen == {"http://a:1", "http://b:2"}


def test_current_before_and_after_next():
    pool = ProxyPool(["a:1", "b:2"])
    # current() before first next() returns the first live proxy, non-destructively.
    assert pool.current() == "http://a:1"
    assert pool.current() == "http://a:1"
    assert pool.next() == "http://a:1"
    assert pool.next() == "http://b:2"
    assert pool.current() == "http://b:2"


# --------------------------------------------------------------------------- #
# ProxyPool — mark_bad skipping
# --------------------------------------------------------------------------- #

def test_mark_bad_is_skipped():
    pool = ProxyPool(["a:1", "b:2", "c:3"])
    pool.mark_bad("b:2")
    assert len(pool) == 2
    got = [pool.next() for _ in range(4)]
    assert "http://b:2" not in got
    assert set(got) == {"http://a:1", "http://c:3"}


def test_mark_bad_accepts_normalized_form():
    pool = ProxyPool(["a:1", "b:2"])
    pool.mark_bad("http://a:1")  # normalized form should also match
    assert len(pool) == 1
    assert pool.next() == "http://b:2"


def test_mark_bad_unknown_proxy_ignored():
    pool = ProxyPool(["a:1"])
    pool.mark_bad("zzz:9")  # not in pool
    assert len(pool) == 1


def test_mark_all_bad_exhausts_pool():
    pool = ProxyPool(["a:1", "b:2"])
    pool.mark_bad("a:1")
    pool.mark_bad("b:2")
    assert pool.next() is None
    assert pool.current() is None
    assert pool.remaining() == 0


# --------------------------------------------------------------------------- #
# len / bool
# --------------------------------------------------------------------------- #

def test_len_and_bool():
    pool = ProxyPool(["a:1", "b:2"])
    assert len(pool) == 2
    assert bool(pool) is True
    pool.mark_bad("a:1")
    assert len(pool) == 1
    assert bool(pool) is True
    pool.mark_bad("b:2")
    assert len(pool) == 0
    assert bool(pool) is False


def test_empty_pool():
    pool = ProxyPool([])
    assert len(pool) == 0
    assert bool(pool) is False
    assert pool.next() is None
    assert pool.current() is None


# --------------------------------------------------------------------------- #
# random strategy
# --------------------------------------------------------------------------- #

def test_random_strategy_returns_live_proxies():
    pool = ProxyPool(["a:1", "b:2", "c:3"], strategy="random")
    for _ in range(20):
        p = pool.next()
        assert p in {"http://a:1", "http://b:2", "http://c:3"}
    # current() agrees with the last random pick.
    assert pool.current() == pool.current()


def test_random_strategy_skips_bad():
    pool = ProxyPool(["a:1", "b:2"], strategy="random")
    pool.mark_bad("a:1")
    for _ in range(10):
        assert pool.next() == "http://b:2"


def test_unknown_strategy_raises():
    with pytest.raises(ValueError):
        ProxyPool(["a:1"], strategy="bogus")


# --------------------------------------------------------------------------- #
# ProxyPool — health state machine (cooldown / backoff / dead / recovery)
# --------------------------------------------------------------------------- #

def test_cooldown_then_half_open_recovery():
    t = {"v": 1000.0}
    pool = ProxyPool(["a:1", "b:2"], now=lambda: t["v"], base_cooldown=30, max_failures=3)
    pool.mark_bad("a:1")
    assert pool.state("a:1") == "cooldown"
    assert pool.remaining() == 1  # only b:2 available now
    t["v"] += 31  # past the cooldown -> half-open -> available again
    assert pool.state("a:1") == "live"
    assert pool.remaining() == 2


def test_exponential_backoff_and_dead_after_max():
    t = {"v": 0.0}
    pool = ProxyPool(["a:1"], now=lambda: t["v"], base_cooldown=10, max_failures=3)
    pool.mark_bad("a:1")  # fail 1 -> cooldown 10s
    assert pool.state("a:1") == "cooldown"
    t["v"] = 9.9
    assert pool.state("a:1") == "cooldown"
    t["v"] = 10.0
    assert pool.state("a:1") == "live"  # half-open
    pool.mark_bad("a:1")  # fail 2 -> cooldown 20s (10 * 2)
    t["v"] = 10.0 + 19.9
    assert pool.state("a:1") == "cooldown"
    t["v"] = 10.0 + 20.0
    assert pool.state("a:1") == "live"
    pool.mark_bad("a:1")  # fail 3 == max -> dead
    assert pool.state("a:1") == "dead"
    assert pool.dead_count() == 1
    assert pool.remaining() == 0


def test_fatal_retires_immediately():
    pool = ProxyPool(["a:1", "b:2"])
    pool.mark_bad("a:1", fatal=True)
    assert pool.state("a:1") == "dead"
    assert pool.remaining() == 1
    assert pool.dead_count() == 1


def test_mark_good_recovers_from_cooldown():
    t = {"v": 0.0}
    pool = ProxyPool(["a:1"], now=lambda: t["v"], base_cooldown=100)
    pool.mark_bad("a:1")
    assert pool.state("a:1") == "cooldown"
    pool.mark_good("a:1")
    assert pool.state("a:1") == "live"
    assert pool.remaining() == 1


def test_cooldown_override_and_normalized_match():
    t = {"v": 0.0}
    pool = ProxyPool(["a:1"], now=lambda: t["v"])
    pool.mark_bad("http://a:1", cooldown=5)  # normalized form matches
    assert pool.state("a:1") == "cooldown"
    t["v"] = 5.0
    assert pool.state("a:1") == "live"
