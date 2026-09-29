"""Camoufox package and browser compatibility checks."""

from __future__ import annotations

import sys
import types

import pytest

from wraith import engine


def test_camoufox_browser_selection_is_pinned() -> None:
    assert engine._CAMOUFOX_BROWSER_VERSION == "152.0.4-beta.30"
    assert engine._camoufox_browser_options() == {
        "browser": "152.0.4-beta.30",
        "ff_version": 152,
    }


def test_missing_camoufox_browser_has_actionable_error(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FakeCamoufox:
        def __init__(self, **options):
            seen.update(options)

        def __enter__(self):
            raise ValueError("Browser version '152.0.4-beta.30' not found")

    module = types.ModuleType("camoufox.sync_api")
    module.Camoufox = FakeCamoufox
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", module)
    monkeypatch.setattr(engine, "_assert_camoufox_playwright_ok", lambda: None)
    monkeypatch.setattr(engine, "_camoufox_pinned_executable", lambda: "/tmp/camoufox")

    with pytest.raises(engine.EngineUnavailableError, match="camoufox fetch official/152.0.4-beta.30"):
        engine._launch_camoufox(
            headless=True,
            geoip=False,
            locale=None,
            timezone=None,
            humanize=False,
            profile_dir=None,
            extra={},
        )

    assert seen["browser"] == "152.0.4-beta.30"
    assert seen["ff_version"] == 152
    assert seen["executable_path"] == "/tmp/camoufox"


def test_missing_pinned_camoufox_binary_does_not_fetch(monkeypatch) -> None:
    multiversion = types.ModuleType("camoufox.multiversion")
    multiversion.find_installed_version = lambda specifier: None
    pkgman = types.ModuleType("camoufox.pkgman")
    pkgman.launch_path = lambda path: "/tmp/camoufox"
    monkeypatch.setitem(sys.modules, "camoufox.multiversion", multiversion)
    monkeypatch.setitem(sys.modules, "camoufox.pkgman", pkgman)

    with pytest.raises(engine.EngineUnavailableError, match="official/152.0.4-beta.30"):
        engine._camoufox_pinned_executable()
