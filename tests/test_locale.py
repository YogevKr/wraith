"""Camoufox gets a stable page language on a direct connection."""

from __future__ import annotations

import sys
import types

import pytest

from wraith import engine


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"LANG": "he_IL.UTF-8"}, "he-IL"),
        ({"LC_ALL": "en_GB.UTF-8", "LANG": "he_IL.UTF-8"}, "en-GB"),
        ({"LANG": "C.UTF-8", "LC_MESSAGES": "de_DE@euro"}, "de-DE"),
    ],
)
def test_system_locale_reads_the_environment(monkeypatch, env, expected):
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert engine._system_locale() == expected


def test_system_locale_is_unknown_without_a_language(monkeypatch):
    for name in ("LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setattr(engine.sys, "platform", "linux")
    assert engine._system_locale() is None


def _camoufox_options(monkeypatch, **kw) -> dict:
    seen: dict = {}

    class FakeCamoufox:
        def __init__(self, **options):
            seen.update(options)

        def __enter__(self):
            raise RuntimeError("stop after the options")

    module = types.ModuleType("camoufox.sync_api")
    module.Camoufox = FakeCamoufox
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", module)
    monkeypatch.setattr(engine, "_assert_camoufox_playwright_ok", lambda: None)
    monkeypatch.setattr(engine, "_ensure_camoufox_bundle_properties", lambda: None)
    monkeypatch.setattr(engine, "_camoufox_pinned_executable", lambda: "/tmp/camoufox")
    monkeypatch.setattr(engine, "_system_locale", lambda: "en-US")
    args = dict(
        headless=True,
        geoip=True,
        locale=None,
        timezone=None,
        humanize=False,
        profile_dir=None,
        extra={},
    )
    args.update(kw)
    with pytest.raises(RuntimeError, match="stop after the options"):
        engine._launch_camoufox(**args)
    return seen


def test_direct_connection_uses_the_system_locale(monkeypatch):
    assert _camoufox_options(monkeypatch)["locale"] == "en-US"


def test_proxy_keeps_the_geoip_language(monkeypatch):
    options = _camoufox_options(monkeypatch, extra={"proxy": {"server": "http://p:1"}})
    assert "locale" not in options


def test_a_caller_locale_wins(monkeypatch):
    assert _camoufox_options(monkeypatch, locale="fr-FR")["locale"] == "fr-FR"
    assert "locale" not in _camoufox_options(monkeypatch, geoip=False)
