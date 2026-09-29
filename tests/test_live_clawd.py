"""Local browser checks: WRAITH_LIVE_TEST=1 uv run pytest tests/test_live_clawd.py."""

import os

import pytest

if os.environ.get("WRAITH_LIVE_TEST") != "1":
    pytest.skip("set WRAITH_LIVE_TEST=1 to run Camoufox checks", allow_module_level=True)

from wraith.agent import AgentBrowser
from wraith.engine import launch
from wraith.secrets import SecretMaterial, register_vault_provider, unregister_vault_provider


@pytest.fixture(scope="module")
def session():
    with launch(engine="camoufox", headless=True, geoip=False, locale="en-US") as session:
        session.context.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))
        yield session


@pytest.fixture
def browser(session, monkeypatch):
    session.page.goto("https://clawd.test/")
    browser = AgentBrowser(session=session)
    monkeypatch.setattr(browser, "_wait_for_settle", lambda: None)
    return browser


def index_for(browser, name, **kwargs):
    return next(e.index for e in browser.snapshot(**kwargs) if e.attributes.get("id") == name)


def test_hidden_and_modal_covered_cart_controls(browser):
    browser.page.set_content(
        '<button style="position:fixed;left:10px;top:10px" id="cart">Cart</button>'
        + '<button style="display:none">AddToCart</button>' * 46
        + '<div style="position:fixed;inset:0;background:white;z-index:9">'
        '<button id="dismiss">Dismiss</button></div>'
    )
    snapshot = browser.snapshot()
    assert [e.attributes.get("id") for e in snapshot] == ["dismiss"]


def test_moved_target_uses_current_position(browser):
    browser.page.set_content('''
        <button id="target" style="position:absolute;left:10px;top:10px"
          onclick="document.body.dataset.clicked='target'">Target</button>
        <button id="other" style="position:absolute;left:300px;top:10px"
          onclick="document.body.dataset.clicked='other'">Other</button>
    ''')
    index = index_for(browser, "target")
    browser.page.evaluate("document.querySelector('#target').style.left='300px';document.querySelector('#other').style.left='10px'")
    browser.click(index)
    assert browser.page.evaluate("document.body.dataset.clicked") == "target"


def test_offscreen_target_scrolls_before_click(browser):
    browser.page.set_content('<button id="target" style="margin-top:4000px" onclick="document.body.dataset.clicked=1">Target</button>')
    index = index_for(browser, "target", viewport_only=False)
    browser.click(index)
    assert browser.page.evaluate("document.body.dataset.clicked") == "1"


@pytest.mark.parametrize("covered", [False, True])
def test_shadow_hit_testing(browser, covered):
    browser.page.set_content('<div id="host"></div>')
    browser.page.evaluate('''() => {
        const root=document.querySelector('#host').attachShadow({mode:'open'});
        root.innerHTML='<button id="target" style="position:fixed;left:10px;top:10px">Target</button>';
        root.querySelector('button').onclick=()=>document.body.dataset.clicked=1;
    }''')
    if covered:
        browser.page.evaluate('''() => {
            const overlay=document.createElement('div');
            overlay.style='position:fixed;inset:0;z-index:99;background:white';
            document.querySelector('#host').shadowRoot.append(overlay);
        }''')
        assert all(e.attributes.get("id") != "target" for e in browser.snapshot())
    else:
        browser.click(index_for(browser, "target"))
        assert browser.page.evaluate("document.body.dataset.clicked") == "1"


def test_pointer_disabled_control_is_absent(browser):
    browser.page.set_content('<button style="pointer-events:none" id="target">Target</button>')
    assert len(browser.snapshot()) == 0


@pytest.mark.parametrize("mutation", ["none", "overlay", "replace", "stale"])
def test_vault_pins_field_and_clears_material(browser, mutation):
    browser.page.set_content('<input type="password" id="password" autocomplete="current-password">')
    index = index_for(browser, "password")
    if mutation == "stale":
        browser.page.evaluate('''() => {
            const input=document.querySelector('input');
            input.removeAttribute('data-wraith-index');
            input.insertAdjacentHTML('beforebegin', '<button>Earlier control</button>');
        }''')

    class Provider:
        material = None

        def authorize_item(self, item, origin):
            return item == "fixture" and origin == "https://clawd.test"

        def resolve_item(self, item, context):
            if mutation == "overlay":
                browser.page.evaluate('''() => {
                    const overlay=document.createElement('div');
                    overlay.style='position:fixed;inset:0;background:white;z-index:99';
                    document.body.append(overlay);
                }''')
            if mutation == "replace":
                browser.page.evaluate("() => { const el=document.querySelector('#password'); el.replaceWith(el.cloneNode(true)); }")
            self.material = SecretMaterial("synthetic-local-canary")
            return self.material

    provider = Provider()
    register_vault_provider("fixture", provider)
    try:
        if mutation in {"overlay", "replace"}:
            with pytest.raises(RuntimeError) as caught:
                browser.fill_vault_item(index, "fixture", field_kind="password", provider="fixture")
            assert caught.value.__context__ is None
            assert browser.page.locator('input').input_value() == ""
        else:
            assert browser.fill_vault_item(index, "fixture", field_kind="password", provider="fixture") is True
            assert browser.page.locator('input').input_value() == "synthetic-local-canary"
        assert provider.material.cleared
    finally:
        unregister_vault_provider("fixture")
