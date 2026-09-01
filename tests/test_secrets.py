"""Offline tests for opaque secret capabilities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import wraith.agent as agent_module
from wraith.agent import AgentBrowser
from wraith.secrets import (
    SecretCapability,
    SecretCapabilityError,
    SecretMaterial,
    SecretPolicyError,
    SecretProviderError,
    get_secret_provider,
    register_secret_provider,
    unregister_secret_provider,
)
from wraith.snapshot import Element, Snapshot


class _FakeLocator:
    def __init__(
        self,
        *,
        tag: str = "input",
        field_type: str = "password",
        autocomplete: str = "current-password",
        fail_fill: bool = False,
    ) -> None:
        self.metadata = {
            "tag": tag,
            "type": field_type,
            "autocomplete": autocomplete,
            "contenteditable": False,
            "disabled": False,
            "readonly": False,
        }
        self.fail_fill = fail_fill
        self.filled = None
        self.secret_marked = False

    def count(self):
        return 1

    def element_handle(self):
        return self

    def evaluate(self, script):
        if "setAttribute" in script:
            self.secret_marked = True
            return None
        return self.metadata

    def fill(self, value):
        if self.fail_fill:
            raise RuntimeError("fill failed")
        self.filled = value


class _FakePage:
    def __init__(self, locator, url="https://login.example.com/sign-in") -> None:
        self.url = url
        self._locator = locator

    def locator(self, _selector):
        return self._locator

    def evaluate(self, *_args):
        return {"url": self.url, "n": 1, "h": 1}

    def title(self):
        return "Login"


class _FakeContext:
    def __init__(self):
        self.saved = None

    def storage_state(self, path=None):
        self.saved = path


class _FakeSession:
    def __init__(self, page, context=None):
        self.page = page
        self.context = context or _FakeContext()


class _Provider:
    def __init__(self, value="synthetic-secret", fail=False):
        self.value = value
        self.fail = fail
        self.calls = []
        self.material = None

    def resolve(self, capability, context):
        self.calls.append((capability, context))
        if self.fail:
            raise RuntimeError(f"provider leaked {self.value}")
        self.material = SecretMaterial(self.value)
        return self.material


def _capability(**changes):
    values = {
        "provider": "test",
        "handle": "opaque-handle",
        "allowed_origins": ("https://login.example.com",),
        "field_kind": "password",
        "max_uses": 1,
    }
    values.update(changes)
    return SecretCapability(**values)


def _agent(monkeypatch, locator=None, provider=None, url=None):
    locator = locator or _FakeLocator()
    provider = provider or _Provider()
    page = _FakePage(locator, url=url or "https://login.example.com/sign-in")
    browser = AgentBrowser(
        session=_FakeSession(page),
        secret_providers={"test": provider},
    )
    monkeypatch.setattr(browser, "_wait_for_settle", lambda: None)
    monkeypatch.setattr(
        agent_module,
        "take_snapshot",
        lambda _page, **_kw: Snapshot(
            page.url,
            "Login",
            [Element(0, "input", "textbox", "", {"type": "password"})],
        ),
    )
    return browser, locator, provider


def test_fill_secret_uses_provider_and_clears_material(monkeypatch):
    browser, locator, provider = _agent(monkeypatch)

    snapshot = browser.fill_secret(0, _capability())

    assert locator.filled == "synthetic-secret"
    assert locator.secret_marked is True
    assert provider.material.cleared is True
    assert browser.secret_tainted is True
    assert snapshot.url == "https://login.example.com/sign-in"
    assert "synthetic-secret" not in snapshot.to_text()


def test_origin_mismatch_fails_before_provider(monkeypatch):
    browser, _, provider = _agent(monkeypatch, url="https://evil.example.net/")

    with pytest.raises(SecretPolicyError, match="origin"):
        browser.fill_secret(0, _capability())

    assert provider.calls == []
    assert browser.secret_tainted is False


def test_expired_capability_fails_before_provider(monkeypatch):
    browser, _, provider = _agent(monkeypatch)
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)

    with pytest.raises(SecretCapabilityError, match="expired"):
        browser.fill_secret(0, _capability(expires_at=expired))

    assert provider.calls == []


def test_field_kind_mismatch_fails_before_provider(monkeypatch):
    locator = _FakeLocator(field_type="text", autocomplete="username")
    browser, _, provider = _agent(monkeypatch, locator=locator)

    with pytest.raises(SecretPolicyError, match="field_kind"):
        browser.fill_secret(0, _capability())

    assert provider.calls == []


def test_capability_use_limit(monkeypatch):
    browser, _, provider = _agent(monkeypatch)
    capability = _capability()
    browser.fill_secret(0, capability)

    with pytest.raises(SecretCapabilityError, match="exhausted"):
        browser.fill_secret(0, capability)

    assert len(provider.calls) == 1


def test_capability_id_cannot_reset_use_limit(monkeypatch):
    browser, _, provider = _agent(monkeypatch)
    browser.fill_secret(0, _capability(capability_id="first"))

    with pytest.raises(SecretCapabilityError, match="exhausted"):
        browser.fill_secret(0, _capability(capability_id="second"))

    assert len(provider.calls) == 1


def test_capability_cannot_raise_use_limit(monkeypatch):
    browser, _, provider = _agent(monkeypatch)
    browser.fill_secret(0, _capability(max_uses=1))

    with pytest.raises(SecretCapabilityError, match="limit increased"):
        browser.fill_secret(0, _capability(max_uses=2))

    assert len(provider.calls) == 1


def test_material_clears_when_browser_fill_fails(monkeypatch):
    locator = _FakeLocator(fail_fill=True)
    browser, _, provider = _agent(monkeypatch, locator=locator)

    with pytest.raises(SecretProviderError, match="browser") as caught:
        browser.fill_secret(0, _capability())

    assert provider.material.cleared is True
    assert browser.secret_tainted is True
    assert "synthetic-secret" not in str(caught.value)
    assert caught.value.__context__ is None
    with pytest.raises(SecretCapabilityError, match="exhausted"):
        browser.fill_secret(0, _capability())


def test_provider_error_does_not_expose_provider_message(monkeypatch):
    provider = _Provider(fail=True)
    browser, _, _ = _agent(monkeypatch, provider=provider)

    with pytest.raises(SecretProviderError) as caught:
        browser.fill_secret(0, _capability())

    assert "synthetic-secret" not in str(caught.value)
    assert caught.value.__context__ is None


def test_storage_export_requires_explicit_override_after_secret(monkeypatch):
    browser, _, _ = _agent(monkeypatch)
    browser.fill_secret(0, _capability())

    with pytest.raises(SecretPolicyError, match="Storage export"):
        browser.save_storage_state("/tmp/state.json")

    assert browser.save_storage_state(
        "/tmp/state.json",
        allow_secret_tainted=True,
    ) == "/tmp/state.json"


def test_secret_representations_are_redacted():
    capability = _capability(handle="handle-must-not-appear")
    material = SecretMaterial("value-must-not-appear")

    assert "handle-must-not-appear" not in repr(capability)
    assert "value-must-not-appear" not in repr(material)


def test_editable_element_value_is_redacted_from_snapshot_text():
    element = Element(
        0,
        "input",
        "textbox",
        "",
        {"type": "text", "value": "value-must-not-appear"},
    )

    assert "value-must-not-appear" not in element.to_text()
    assert "value-must-not-appear" not in element.signature


@pytest.mark.parametrize(
    ("tag", "role", "attributes"),
    [
        ("textarea", "textbox", {}),
        ("div", "textbox", {"contenteditable": True}),
    ],
)
def test_editable_element_safe_label_is_preserved(tag, role, attributes):
    element = Element(0, tag, role, "Safe accessible label", attributes)

    assert "Safe accessible label" in element.to_text()


def test_checkbox_value_remains_in_snapshot_metadata():
    element = Element(
        0,
        "input",
        "checkbox",
        "",
        {"type": "checkbox", "value": "accept-terms"},
    )

    assert 'value="accept-terms"' in element.to_text()
    assert "value=accept-terms" in element.signature


@pytest.mark.parametrize("field_type", ["date", "time", "month", "week", "custom-type"])
def test_other_editable_input_values_are_redacted(field_type):
    element = Element(
        0,
        "input",
        "textbox",
        "Safe field label",
        {"type": field_type, "value": "value-must-not-appear"},
    )

    assert "value-must-not-appear" not in element.to_text()
    assert "value-must-not-appear" not in element.signature
    assert "Safe field label" in element.signature


def test_global_provider_registry():
    provider = _Provider()
    register_secret_provider("registry-test", provider)
    try:
        assert get_secret_provider("registry-test") is provider
        with pytest.raises(ValueError, match="already registered"):
            register_secret_provider("registry-test", provider)
    finally:
        unregister_secret_provider("registry-test")

    with pytest.raises(SecretProviderError, match="not registered"):
        get_secret_provider("registry-test")


def test_ipv6_origin_keeps_brackets():
    capability = _capability(allowed_origins=("https://[::1]:8443/login",))

    assert capability.allowed_origins == ("https://[::1]:8443",)


def test_origin_normalizes_expanded_ipv6():
    capability = _capability(
        allowed_origins=("https://[0:0:0:0:0:0:0:1]:8443/login",)
    )

    assert capability.allowed_origins == ("https://[::1]:8443",)


def test_origin_normalizes_unicode_domain():
    capability = _capability(allowed_origins=("https://täst.example/login",))

    assert capability.allowed_origins == ("https://xn--tst-qla.example",)


def test_capability_from_dict_normalizes_origin_and_expiry():
    capability = SecretCapability.from_dict(
        {
            "provider": "test",
            "handle": "opaque",
            "allowed_origins": ["HTTPS://EXAMPLE.COM:443/path"],
            "field_kind": "card_number",
            "expires_at": "2030-01-01T00:00:00Z",
        }
    )

    assert capability.allowed_origins == ("https://example.com",)
    assert capability.field_kind == "card-number"
    assert capability.expires_at.tzinfo is not None


@pytest.mark.parametrize("max_uses", [True, 0, -1, 1.5, "1"])
def test_capability_rejects_invalid_use_limit(max_uses):
    with pytest.raises(SecretCapabilityError, match="max_uses"):
        _capability(max_uses=max_uses)


def test_capability_rejects_unknown_field_kind():
    with pytest.raises(SecretCapabilityError, match="field_kind"):
        _capability(field_kind="unknown")


def test_generic_editors_only_accept_text_kind():
    metadata = {
        "tag": "textarea",
        "type": "",
        "autocomplete": "",
        "contenteditable": "false",
    }

    assert AgentBrowser._secret_field_matches("text", metadata) is True
    assert AgentBrowser._secret_field_matches("username", metadata) is False
    assert AgentBrowser._secret_field_matches("otp", metadata) is False


@pytest.mark.parametrize("field_type", ["password", "email", "tel", "number", "search", "url"])
def test_text_kind_rejects_stronger_input_types(field_type):
    metadata = {
        "tag": "input",
        "type": field_type,
        "autocomplete": "",
        "contenteditable": "false",
    }

    assert AgentBrowser._secret_field_matches("text", metadata) is False


def test_text_kind_rejects_stronger_autocomplete_semantics():
    metadata = {
        "tag": "input",
        "type": "text",
        "autocomplete": "section-login username",
        "contenteditable": "false",
    }

    assert AgentBrowser._secret_field_matches("text", metadata) is False


def test_contenteditable_target_is_rejected():
    metadata = {
        "tag": "div",
        "type": "",
        "autocomplete": "",
        "contenteditable": "true",
    }

    assert AgentBrowser._secret_field_matches("text", metadata) is False


@pytest.mark.parametrize("field_type", ["checkbox", "radio", "file", "button"])
def test_non_fillable_input_types_are_rejected(field_type):
    metadata = {
        "tag": "input",
        "type": field_type,
        "autocomplete": "current-password",
        "contenteditable": "false",
        "disabled": "false",
        "readonly": "false",
    }

    assert AgentBrowser._secret_field_matches("password", metadata) is False


@pytest.mark.parametrize("state", ["disabled", "readonly"])
def test_non_writable_fields_are_rejected(state):
    metadata = {
        "tag": "input",
        "type": "password",
        "autocomplete": "current-password",
        "contenteditable": "false",
        "disabled": "false",
        "readonly": "false",
    }
    metadata[state] = "true"

    assert AgentBrowser._secret_field_matches("password", metadata) is False


def test_autocomplete_token_lists_are_supported():
    metadata = {
        "tag": "input",
        "type": "text",
        "autocomplete": "section-payment billing cc-number",
        "contenteditable": "false",
    }

    assert AgentBrowser._secret_field_matches("card-number", metadata) is True


def test_month_input_supports_card_expiry():
    metadata = {
        "tag": "input",
        "type": "month",
        "autocomplete": "billing cc-exp",
        "contenteditable": "false",
    }

    assert AgentBrowser._secret_field_matches("card-expiry", metadata) is True


@pytest.mark.parametrize("kind", ["username", "otp", "text"])
def test_conflicting_autocomplete_semantics_are_rejected(kind):
    metadata = {
        "tag": "input",
        "type": "text",
        "autocomplete": "current-password",
        "contenteditable": "false",
    }

    assert AgentBrowser._secret_field_matches(kind, metadata) is False


@pytest.mark.parametrize("kind", ["username", "otp"])
def test_username_and_otp_require_autocomplete(kind):
    metadata = {
        "tag": "input",
        "type": "text",
        "autocomplete": "",
        "contenteditable": "false",
        "disabled": "false",
        "readonly": "false",
    }

    assert AgentBrowser._secret_field_matches(kind, metadata) is False


def test_origin_is_rechecked_after_provider_resolution(monkeypatch):
    browser, locator, provider = _agent(monkeypatch)

    original_resolve = provider.resolve

    def navigate_during_resolve(capability, context):
        material = original_resolve(capability, context)
        browser.page.url = "https://evil.example.net/"
        return material

    provider.resolve = navigate_during_resolve

    with pytest.raises(SecretPolicyError, match="changed"):
        browser.fill_secret(0, _capability())

    assert locator.filled is None
    assert provider.material.cleared is True
    assert browser.secret_tainted is False


def test_expiry_is_rechecked_after_provider_resolution(monkeypatch):
    browser, locator, provider = _agent(monkeypatch)
    before = datetime(2030, 1, 1, tzinfo=timezone.utc)
    expiry = before + timedelta(seconds=1)
    after = expiry + timedelta(seconds=1)

    class _Clock:
        values = iter((before, after))

        @classmethod
        def now(cls, _tz):
            return next(cls.values)

    monkeypatch.setattr(agent_module, "datetime", _Clock)

    with pytest.raises(SecretCapabilityError, match="expired during use"):
        browser.fill_secret(0, _capability(expires_at=expiry))

    assert locator.filled is None
    assert provider.material.cleared is True
    assert browser.secret_tainted is False


def test_screenshot_blocked_after_secret_fill(monkeypatch):
    browser, _, _ = _agent(monkeypatch)
    browser.fill_secret(0, _capability())

    with pytest.raises(SecretPolicyError, match="Screenshots"):
        browser.screenshot()


def test_highlighted_snapshot_blocked_after_secret_fill(monkeypatch):
    browser, _, _ = _agent(monkeypatch)
    browser.fill_secret(0, _capability())

    with pytest.raises(SecretPolicyError, match="Highlighted snapshots"):
        browser.snapshot(highlight=True)


def test_secret_policy_state_is_shared_by_session_context(monkeypatch):
    locator = _FakeLocator()
    provider = _Provider()
    page = _FakePage(locator)
    session = _FakeSession(page)
    first = AgentBrowser(session=session, secret_providers={"test": provider})
    second = AgentBrowser(session=session, secret_providers={"test": provider})
    monkeypatch.setattr(first, "_wait_for_settle", lambda: None)
    monkeypatch.setattr(
        agent_module,
        "take_snapshot",
        lambda _page, **_kw: Snapshot(page.url, "Login", []),
    )

    first.fill_secret(0, _capability())

    assert second.secret_tainted is True
    with pytest.raises(SecretPolicyError, match="Storage export"):
        second.save_storage_state("/tmp/state.json")
    with pytest.raises(SecretCapabilityError, match="exhausted"):
        second.fill_secret(0, _capability())
