"""Offline tests for the Chromium launch identity: channel, sandbox, User-Agent, window.

No browser starts here; a fake patchright records what the launcher asks for.
"""

from __future__ import annotations

import sys
import types
import warnings

import pytest

from wraith import engine

HEADLESS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/155.0.0.0 Safari/537.36"
)
CLEAN_UA = HEADLESS_UA.replace("HeadlessChrome/", "Chrome/")


class _Page:
    def __init__(self, user_agent):
        self.user_agent = user_agent

    def evaluate(self, script):
        return self.user_agent


class _Context:
    def __init__(self, log, kw):
        self.log, self.kw, self.pages = log, kw, []

    def new_page(self):
        return _Page(self.kw.get("user_agent", HEADLESS_UA))

    def close(self):
        self.log.append("context.close")


class _Browser:
    def __init__(self, log, kw):
        self.log, self.kw = log, kw

    def new_context(self, **kw):
        self.log.append(("new_context", kw))
        return _Context(self.log, kw)

    def close(self):
        self.log.append("browser.close")


class _Chromium:
    def __init__(self, log, fail_when=None):
        self.log, self.fail_when = log, fail_when

    def launch(self, **kw):
        return _Browser(self.log, self._start("launch", kw))

    def launch_persistent_context(self, profile_dir, **kw):
        return _Context(self.log, self._start("launch_persistent_context", kw))

    def _start(self, name, kw):
        self.log.append((name, kw))
        if self.fail_when and self.fail_when(kw):
            raise RuntimeError("Chromium distribution 'chrome' is not found")
        return kw


@pytest.fixture
def fake_pw(monkeypatch):
    log: list = []
    state = {"fail_when": None}

    class _Starter:
        def start(self):
            pw = types.SimpleNamespace(chromium=_Chromium(log, state["fail_when"]))
            pw.stop = lambda: log.append("pw.stop")
            return pw

    module = types.ModuleType("patchright.sync_api")
    module.sync_playwright = _Starter
    monkeypatch.setitem(sys.modules, "patchright.sync_api", module)
    monkeypatch.setattr(engine, "_HEADLESS_UA_CACHE", {})
    monkeypatch.setattr(
        engine, "_installed_chrome", lambda: "/Applications/Google Chrome"
    )
    monkeypatch.setattr(engine, "_chromium_sandbox_default", lambda: True)
    # Deterministic GPU assumption; the GPU-specific tests override it.
    monkeypatch.setattr(engine, "_host_has_gpu", lambda: True)
    return log, state


def _launch(**extra):
    headless = extra.pop("headless", True)
    profile_dir = extra.pop("profile_dir", None)
    geoip = extra.pop("geoip", False)
    locale = extra.pop("locale", None)
    timezone = extra.pop("timezone", None)
    return engine._launch_chromium(
        headless=headless,
        geoip=geoip,
        locale=locale,
        timezone=timezone,
        profile_dir=profile_dir,
        extra=extra,
    )


def _calls(log, name):
    return [
        kw
        for entry in log
        if isinstance(entry, tuple) and entry[0] == name
        for kw in [entry[1]]
    ]


def test_geoip_gives_chromium_a_matching_locale_and_timezone(fake_pw, monkeypatch):
    log, _ = fake_pw
    monkeypatch.setattr(
        engine, "_chromium_geoip_identity", lambda proxy: ("en-US", "America/New_York")
    )
    _launch(geoip=True, proxy={"server": "http://p:1"})
    _probe, ctx = _calls(log, "new_context")
    assert ctx["locale"] == "en-US"
    assert ctx["timezone_id"] == "America/New_York"


def test_explicit_locale_and_timezone_win_over_geoip(fake_pw, monkeypatch):
    log, _ = fake_pw
    monkeypatch.setattr(
        engine, "_chromium_geoip_identity", lambda proxy: ("en-US", "America/New_York")
    )
    _launch(geoip=True, locale="he-IL", timezone="Asia/Jerusalem")
    _probe, ctx = _calls(log, "new_context")
    assert ctx["locale"] == "he-IL"
    assert ctx["timezone_id"] == "Asia/Jerusalem"


def test_geoip_failure_keeps_host_defaults(fake_pw, monkeypatch):
    log, _ = fake_pw
    monkeypatch.setattr(engine, "_chromium_geoip_identity", lambda proxy: (None, None))
    _launch(geoip=True)
    _probe, ctx = _calls(log, "new_context")
    assert "locale" not in ctx and "timezone_id" not in ctx


def test_webrtc_is_forced_through_the_proxy(fake_pw):
    log, _ = fake_pw
    _launch(proxy={"server": "http://p:1"})
    (launch_kw,) = _calls(log, "launch")
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in launch_kw["args"]


def test_no_webrtc_flag_without_a_proxy(fake_pw):
    log, _ = fake_pw
    _launch()
    (launch_kw,) = _calls(log, "launch")
    assert "args" not in launch_kw


def test_gpu_less_headless_adds_swiftshader(fake_pw, monkeypatch):
    log, _ = fake_pw
    monkeypatch.setattr(engine, "_host_has_gpu", lambda: False)
    _launch()
    (launch_kw,) = _calls(log, "launch")
    assert "--use-angle=swiftshader" in launch_kw["args"]


def test_gpu_host_keeps_the_native_renderer(fake_pw):
    log, _ = fake_pw
    _launch()
    (launch_kw,) = _calls(log, "launch")
    assert "args" not in launch_kw


def test_headed_skips_swiftshader(fake_pw, monkeypatch):
    log, _ = fake_pw
    monkeypatch.setattr(engine, "_host_has_gpu", lambda: False)
    _launch(headless=False)
    (launch_kw,) = _calls(log, "launch")
    assert "args" not in launch_kw


def test_caller_gl_choice_wins(fake_pw, monkeypatch):
    log, _ = fake_pw
    monkeypatch.setattr(engine, "_host_has_gpu", lambda: False)
    _launch(args=["--use-angle=vulkan"])
    (launch_kw,) = _calls(log, "launch")
    assert launch_kw["args"] == ["--use-angle=vulkan"]


def test_caller_webrtc_policy_wins(fake_pw):
    log, _ = fake_pw
    _launch(proxy={"server": "http://p:1"}, args=["--webrtc-ip-handling-policy=default"])
    (launch_kw,) = _calls(log, "launch")
    assert launch_kw["args"] == ["--webrtc-ip-handling-policy=default"]


def test_geoip_resolves_locale_from_the_exit_ip(monkeypatch):
    import types as _types

    geo = _types.SimpleNamespace(
        locale=_types.SimpleNamespace(language="he", region="IL"),
        timezone="Asia/Jerusalem",
    )
    seen: dict = {}
    fake = _types.ModuleType("camoufox.ip")
    fake.Proxy = lambda **kw: _types.SimpleNamespace(as_string=lambda: "http://p:1")
    fake.public_ip = lambda url: "1.2.3.4"
    fake_geo = _types.ModuleType("camoufox.geolocation")

    def _geo(ip):
        seen["ip"] = ip
        return geo

    fake_geo.get_geolocation = _geo
    monkeypatch.setitem(sys.modules, "camoufox.ip", fake)
    monkeypatch.setitem(sys.modules, "camoufox.geolocation", fake_geo)

    assert engine._chromium_geoip_identity({"server": "http://p:1"}) == (
        "he-IL",
        "Asia/Jerusalem",
    )
    assert seen["ip"] == "1.2.3.4"


def test_headless_uses_installed_chrome_with_a_clean_user_agent(fake_pw):
    log, _ = fake_pw
    _launch()

    (launch_kw,) = _calls(log, "launch")
    assert launch_kw["channel"] == "chrome" and launch_kw["chromium_sandbox"] is True
    probe_ctx, real_ctx = _calls(log, "new_context")
    assert probe_ctx == {}  # the blank probe page that reads the real User-Agent
    assert real_ctx["user_agent"] == CLEAN_UA
    assert real_ctx["viewport"] == {"width": 1920, "height": 993}
    assert real_ctx["screen"] == {"width": 1920, "height": 1080}


def test_headless_without_chrome_runs_full_chromium(fake_pw, monkeypatch):
    log, _ = fake_pw
    monkeypatch.setattr(engine, "_installed_chrome", lambda: None)
    _launch()
    assert _calls(log, "launch")[0]["channel"] == "chromium"


def test_headed_keeps_window_and_user_agent(fake_pw, monkeypatch):
    log, _ = fake_pw
    monkeypatch.setattr(engine, "_installed_chrome", lambda: None)
    _launch(headless=False)

    (launch_kw,) = _calls(log, "launch")
    assert "channel" not in launch_kw
    (ctx,) = _calls(log, "new_context")
    assert ctx == {"no_viewport": True}


def test_caller_options_win(fake_pw):
    log, _ = fake_pw
    _launch(
        channel="msedge",
        chromium_sandbox=False,
        user_agent="custom",
        viewport={"width": 800, "height": 600},
    )

    (launch_kw,) = _calls(log, "launch")
    assert launch_kw["channel"] == "msedge" and launch_kw["chromium_sandbox"] is False
    (ctx,) = _calls(log, "new_context")
    assert ctx == {"user_agent": "custom", "viewport": {"width": 800, "height": 600}}


def test_persistent_profile_reads_user_agent_from_a_probe_and_sizes_the_window(fake_pw):
    log, _ = fake_pw
    _launch(
        profile_dir="/tmp/profile", proxy={"server": "http://p:1"}, args=["--lang=en"]
    )

    (probe_kw,) = _calls(log, "launch")
    assert "proxy" not in probe_kw and probe_kw["headless"] is True
    assert "browser.close" in log
    (kw,) = _calls(log, "launch_persistent_context")
    assert kw["user_agent"] == CLEAN_UA and kw["no_viewport"] is True
    assert kw["proxy"] == {"server": "http://p:1"}
    assert kw["args"] == [
        "--lang=en",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--window-size=1920,1080",
        "--screen-info={0,0 1920x1080}",
    ]


def test_user_agent_probe_is_cached_per_binary(fake_pw):
    log, _ = fake_pw
    _launch(profile_dir="/tmp/a")
    _launch(profile_dir="/tmp/b")
    assert len(_calls(log, "launch")) == 1  # one probe for both launches


def test_failed_defaults_fall_back_to_playwright_defaults(fake_pw):
    log, state = fake_pw
    state["fail_when"] = lambda kw: kw.get("channel") == "chrome"

    with pytest.warns(RuntimeWarning, match="retrying without them"):
        session = _launch(headless=False)

    first, second = _calls(log, "launch")
    assert first["channel"] == "chrome"
    assert "channel" not in second and "chromium_sandbox" not in second
    assert session.engine == "chromium"


def test_failure_without_defaults_is_raised(fake_pw):
    log, state = fake_pw
    state["fail_when"] = lambda kw: True

    with pytest.raises(RuntimeError, match="not found"):
        _launch(channel="chrome", chromium_sandbox=True, headless=False)
    assert "pw.stop" in log


@pytest.mark.parametrize(
    "platform, euid, expected",
    [
        ("darwin", None, True),
        ("win32", None, True),
        ("linux", 1000, True),
        ("linux", 0, False),
    ],
)
def test_sandbox_default(monkeypatch, platform, euid, expected):
    monkeypatch.setattr(engine.sys, "platform", platform)
    monkeypatch.setattr(engine.os, "geteuid", lambda: euid, raising=False)
    assert engine._chromium_sandbox_default() is expected


def test_camoufox_ff_version_warning_is_silenced_but_others_pass():
    class _Cm:
        def __enter__(self):
            warnings.warn(
                "Spoofing the Firefox version will likely lead to detection.",
                RuntimeWarning,
            )
            warnings.warn("When using a proxy, pass geoip=True.", RuntimeWarning)
            return "browser"

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        assert engine._enter_camoufox(_Cm()) == "browser"

    assert [str(w.message) for w in seen] == ["When using a proxy, pass geoip=True."]
