"""Camoufox package and browser compatibility checks."""

from __future__ import annotations

import sys
import types

import pytest

from wraith import engine


def test_camoufox_browser_selection_is_pinned() -> None:
    assert engine._CAMOUFOX_BROWSER_VERSION == "158.0-beta.40"
    assert engine._camoufox_browser_options() == {
        "browser": "158.0-beta.40",
        "ff_version": 158,
    }


def _launch_missing_camoufox(monkeypatch, on_options) -> None:
    """Launch through a fake Camoufox that reports a missing pinned build."""

    class FakeCamoufox:
        def __init__(self, **options):
            on_options(options)

        def __enter__(self):
            raise ValueError("Browser version '158.0-beta.40' not found")

    module = types.ModuleType("camoufox.sync_api")
    module.Camoufox = FakeCamoufox
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", module)
    monkeypatch.setattr(engine, "_assert_camoufox_playwright_ok", lambda: None)
    monkeypatch.setattr(engine, "_camoufox_pinned_executable", lambda: "/tmp/camoufox")

    with pytest.raises(engine.EngineUnavailableError, match="camoufox fetch official/158.0-beta.40"):
        engine._launch_camoufox(
            headless=True,
            geoip=False,
            locale=None,
            timezone=None,
            humanize=False,
            profile_dir=None,
            extra={},
        )


def test_missing_camoufox_browser_has_actionable_error(monkeypatch) -> None:
    seen: dict[str, object] = {}

    _launch_missing_camoufox(monkeypatch, seen.update)

    # 0.5.9b5 rejects browser= and executable_path= together; the pinned
    # executable already selects the build, so browser is dropped.
    assert "browser" not in seen
    assert seen["ff_version"] == 158
    assert seen["executable_path"] == "/tmp/camoufox"


def test_missing_pinned_camoufox_binary_does_not_fetch(monkeypatch) -> None:
    multiversion = types.ModuleType("camoufox.multiversion")
    multiversion.find_installed_version = lambda specifier: None
    pkgman = types.ModuleType("camoufox.pkgman")
    pkgman.launch_path = lambda path: "/tmp/camoufox"
    monkeypatch.setitem(sys.modules, "camoufox.multiversion", multiversion)
    monkeypatch.setitem(sys.modules, "camoufox.pkgman", pkgman)

    with pytest.raises(engine.EngineUnavailableError, match="official/158.0-beta.40"):
        engine._camoufox_pinned_executable()


def _fake_bundle(root, *, beside: bool) -> object:
    """Build a Camoufox install with macOS or Linux file layout."""
    import json

    app = root / "Camoufox.app" / "Contents"
    exe = app / "MacOS" / "camoufox"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    props = exe.parent if beside else app / "Resources"
    props.mkdir(parents=True, exist_ok=True)
    (props / "properties.json").write_text(
        json.dumps([{"property": "navigator.userAgent", "type": "str"}])
    )
    return exe


def test_macos_bundle_properties_load_from_resources(tmp_path, monkeypatch) -> None:
    from camoufox import utils as camoufox_utils

    # Let monkeypatch restore the original loader after the shim replaces it.
    monkeypatch.setattr(camoufox_utils, "_load_properties", camoufox_utils._load_properties)
    exe = _fake_bundle(tmp_path, beside=False)

    engine._ensure_camoufox_bundle_properties()
    engine._ensure_camoufox_bundle_properties()

    camoufox_utils.validate_config({"navigator.userAgent": "Mozilla/5.0"}, path=exe)
    with pytest.raises(camoufox_utils.InvalidPropertyType):
        camoufox_utils.validate_config({"navigator.userAgent": 1}, path=exe)


def test_properties_beside_executable_take_precedence(tmp_path, monkeypatch) -> None:
    import json

    from camoufox import utils as camoufox_utils

    monkeypatch.setattr(camoufox_utils, "_load_properties", camoufox_utils._load_properties)
    exe = _fake_bundle(tmp_path, beside=True)
    resources = exe.parent.parent / "Resources"
    resources.mkdir()
    (resources / "properties.json").write_text(json.dumps([]))

    engine._ensure_camoufox_bundle_properties()

    # Camoufox 0.5.8 returns {property: {property, type}}; 0.5.6 returned
    # {property: type}. The beside file must win over the empty Resources file.
    result = camoufox_utils._load_properties(path=exe)
    assert "navigator.userAgent" in result


def test_camoufox_launch_applies_bundle_fix(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(engine, "_ensure_camoufox_bundle_properties", lambda: calls.append(True))

    def check_order(options) -> None:
        assert calls, "bundle fix must run before Camoufox validates options"

    _launch_missing_camoufox(monkeypatch, check_order)

    assert calls == [True]
