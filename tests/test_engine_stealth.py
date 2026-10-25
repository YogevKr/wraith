"""Offline tests for engine stealth helpers added after validation.

Covers: stable per-profile fingerprint preset, SOCKS remote-DNS pref, wheel in
the behavioral nudge, and the Turnstile auto-click. No browser starts.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from wraith import engine


# --------------------------------------------------------------------------- #
# Per-profile fingerprint preset
# --------------------------------------------------------------------------- #

def test_profile_fingerprint_preset_is_created_and_reused(tmp_path):
    first = engine._profile_fingerprint_preset(str(tmp_path))
    assert isinstance(first, dict) and first
    path = tmp_path / engine._PROFILE_FINGERPRINT_FILE
    assert path.exists()
    assert json.loads(path.read_text()) == first

    # Second call reuses the stored device; it must not regenerate.
    second = engine._profile_fingerprint_preset(str(tmp_path))
    assert second == first


def test_profile_fingerprint_preset_tolerates_a_corrupt_file(tmp_path):
    (tmp_path / engine._PROFILE_FINGERPRINT_FILE).write_text("{not json")
    preset = engine._profile_fingerprint_preset(str(tmp_path))
    assert isinstance(preset, dict) and preset


def test_profile_fingerprint_preset_returns_none_on_failure(tmp_path, monkeypatch):
    import camoufox.fingerprints as fp

    monkeypatch.setattr(fp, "get_random_preset", lambda **kw: None)
    assert engine._profile_fingerprint_preset(str(tmp_path)) is None


# --------------------------------------------------------------------------- #
# SOCKS proxy -> remote DNS
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "proxy, expected",
    [
        ({"server": "socks5://h:1"}, True),
        ({"server": "socks5h://h:1"}, True),
        ({"server": "socks4://h:1"}, True),
        ({"server": "http://h:1"}, False),
        (None, False),
        ({}, False),
    ],
)
def test_is_socks_proxy(proxy, expected):
    assert engine._is_socks_proxy(proxy) is expected


def _capture_launch_options(monkeypatch, seen, **kw):
    class FakeCamoufox:
        def __init__(self, **options):
            seen.update(options)

        def __enter__(self):
            raise ValueError("Browser version '156.0.1-beta.36' not found")

    module = types.ModuleType("camoufox.sync_api")
    module.Camoufox = FakeCamoufox
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", module)
    monkeypatch.setattr(engine, "_assert_camoufox_playwright_ok", lambda: None)
    monkeypatch.setattr(engine, "_camoufox_pinned_executable", lambda: "/tmp/camoufox")

    with pytest.raises(engine.EngineUnavailableError):
        engine._launch_camoufox(
            headless=True,
            geoip=False,
            locale=None,
            timezone=None,
            humanize=False,
            profile_dir=kw.pop("profile_dir", None),
            extra=kw,
        )


def test_socks_proxy_sets_remote_dns_pref(monkeypatch):
    seen: dict = {}
    _capture_launch_options(
        monkeypatch, seen, proxy={"server": "socks5://h:1"}
    )
    assert seen["firefox_user_prefs"]["network.proxy.socks_remote_dns"] is True


def test_http_proxy_does_not_set_remote_dns_pref(monkeypatch):
    seen: dict = {}
    _capture_launch_options(monkeypatch, seen, proxy={"server": "http://h:1"})
    assert "network.proxy.socks_remote_dns" not in seen.get("firefox_user_prefs", {})


def test_profile_dir_adds_a_stable_fingerprint_preset(monkeypatch, tmp_path):
    seen: dict = {}
    _capture_launch_options(monkeypatch, seen, profile_dir=str(tmp_path))
    assert seen["persistent_context"] is True
    assert isinstance(seen["fingerprint_preset"], dict)
    assert (tmp_path / engine._PROFILE_FINGERPRINT_FILE).exists()


def test_caller_fingerprint_preset_wins(monkeypatch, tmp_path):
    seen: dict = {}
    _capture_launch_options(
        monkeypatch, seen, profile_dir=str(tmp_path), fingerprint_preset={"x": 1}
    )
    assert seen["fingerprint_preset"] == {"x": 1}


# --------------------------------------------------------------------------- #
# Behavioral nudge emits a wheel
# --------------------------------------------------------------------------- #

class _NudgeMouse:
    def __init__(self):
        self.wheels = []

    def move(self, x, y, steps=1):
        pass

    def wheel(self, dx, dy):
        self.wheels.append((dx, dy))


class _NudgePage:
    def __init__(self):
        self.mouse = _NudgeMouse()

    def evaluate(self, script):
        return [1366, 768]

    def wait_for_timeout(self, _ms):
        pass


def test_behavioral_nudge_emits_wheel_and_move():
    page = _NudgePage()
    engine._behavioral_nudge(page)
    assert page.mouse.wheels  # a wheel notch reaches the page
    assert any(dy != 0 for _, dy in page.mouse.wheels)
    assert sum(dy for _, dy in page.mouse.wheels) == 0  # net zero scroll


# --------------------------------------------------------------------------- #
# Turnstile auto-click
# --------------------------------------------------------------------------- #

class _TurnstileElement:
    def __init__(self, box):
        self._box = box

    def bounding_box(self):
        return self._box


class _TurnstileFrame:
    def __init__(self, url, box):
        self.url = url
        self._box = box

    def frame_element(self):
        return _TurnstileElement(self._box)


class _TurnstileMouse:
    def __init__(self):
        self.clicks = []

    def move(self, x, y, steps=1):
        pass

    def click(self, x, y, **_kw):
        self.clicks.append((x, y))


class _TurnstilePage:
    def __init__(self, frames):
        self.frames = frames
        self.mouse = _TurnstileMouse()

    def evaluate(self, script):
        return [1366, 768]

    def wait_for_timeout(self, _ms):
        pass


def test_click_visible_turnstile_aims_at_the_checkbox():
    box = {"x": 100.0, "y": 200.0, "width": 400.0, "height": 100.0}
    page = _TurnstilePage([_TurnstileFrame("https://x/challenges.cloudflare.com/turnstile/f/", box)])

    assert engine._click_visible_turnstile(page) is True
    (x, y), = page.mouse.clicks
    assert x == pytest.approx(100 + 0.11 * 400)
    assert y == pytest.approx(200 + 0.45 * 100)


def test_click_visible_turnstile_skips_the_hidden_duplicate():
    hidden = {"x": 0.0, "y": -100.0, "width": 400.0, "height": 100.0}
    visible = {"x": 10.0, "y": 10.0, "width": 400.0, "height": 100.0}
    page = _TurnstilePage(
        [
            _TurnstileFrame("https://x/challenges.cloudflare.com/turnstile/f/", hidden),
            _TurnstileFrame("https://x/challenges.cloudflare.com/turnstile/f/", visible),
        ]
    )

    assert engine._click_visible_turnstile(page) is True
    (x, y), = page.mouse.clicks
    assert (x, y) == pytest.approx((10 + 0.11 * 400, 10 + 0.45 * 100))


def test_click_visible_turnstile_reports_no_frame():
    page = _TurnstilePage([_TurnstileFrame("https://example.com/", {"x": 0, "y": 0, "width": 9, "height": 9})])
    assert engine._click_visible_turnstile(page) is False
    assert page.mouse.clicks == []


# --------------------------------------------------------------------------- #
# Indexed click lands off-center but on the element
# --------------------------------------------------------------------------- #

class _OffsetTarget:
    def __init__(self, hit):
        self._hit = hit

    def bounding_box(self):
        return {"x": 0.0, "y": 0.0, "width": 200.0, "height": 100.0}

    def evaluate(self, _js, _arg=None):
        return self._hit


def test_offset_click_point_stays_inside_the_element():
    from wraith.agent import AgentBrowser

    ab = AgentBrowser()
    for _ in range(20):
        x, y = ab._offset_click_point(_OffsetTarget(True), {"x": 100.0, "y": 50.0})
        assert 100 - 0.22 * 200 <= x <= 100 + 0.22 * 200
        assert 50 - 0.22 * 100 <= y <= 50 + 0.22 * 100


def test_offset_click_point_falls_back_to_center_when_not_hit():
    from wraith.agent import AgentBrowser

    ab = AgentBrowser()
    assert ab._offset_click_point(_OffsetTarget(False), {"x": 100.0, "y": 50.0}) == (
        100.0,
        50.0,
    )


def test_offset_click_point_accepts_a_pair():
    from wraith.agent import AgentBrowser

    ab = AgentBrowser()
    assert ab._offset_click_point(_OffsetTarget(False), (10.0, 20.0)) == (10.0, 20.0)
