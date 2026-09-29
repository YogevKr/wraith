"""Regression tests for Clawd's opaque, interactive, managed browser flow."""

from __future__ import annotations

import json
import subprocess

import pytest

import wraith.snapshot as snapshot_module
from wraith.agent import AgentBrowser
from wraith.managed import ManagedRunError, run_flow
from wraith.redaction import redacted_exception, redact_text
from wraith.secrets import (
    SecretMaterial,
    register_vault_provider,
    unregister_vault_provider,
)
from wraith.snapshot import Element, Snapshot, _parse_elements


class _Locator:
    def __init__(self) -> None:
        self.filled = None

    def count(self):
        return 1

    def element_handle(self):
        return self

    def evaluate(self, script):
        if "elementFromPoint" in script:
            return {"x": 70.0, "y": 50.0}
        if "setAttribute" in script:
            return None
        return {
            "tag": "input",
            "type": "password",
            "autocomplete": "current-password",
            "contenteditable": False,
            "disabled": False,
            "readonly": False,
        }

    def scroll_into_view_if_needed(self):
        pass

    def bounding_box(self):
        return {"x": 20.0, "y": 30.0, "width": 100.0, "height": 40.0}

    def click(self, **_kwargs):
        pass

    def fill(self, value):
        self.filled = value


class _Mouse:
    def __init__(self):
        self.clicks = []

    def click(self, x, y):
        self.clicks.append((x, y))


class _Page:
    url = "https://shop.example/checkout"

    def __init__(self):
        self.mouse = _Mouse()
        self.loc = _Locator()

    def locator(self, _selector):
        return self.loc

    def wait_for_load_state(self, *_args, **_kwargs):
        return None

    def wait_for_timeout(self, *_args, **_kwargs):
        return None

    def evaluate(self, script, *args):
        if args and isinstance(args[0], int):
            return {"x": 70.0, "y": 50.0}
        if "location.href" in script:
            return {"url": self.url, "n": 1, "h": 1}
        return [{
            "index": 3,
            "tag": "input",
            "role": "textbox",
            "text": "Password",
            "attributes": {},
            "bounds": {"x": 20, "y": 30, "width": 100, "height": 40},
        }]


class _Context:
    pass


class _Session:
    def __init__(self, page):
        self.page = page
        self.context = _Context()


def _browser():
    page = _Page()
    browser = AgentBrowser(session=_Session(page))
    browser.last_snapshot = Snapshot(
        page.url,
        "Checkout",
        [Element(3, "input", "textbox", "Password", {}, x=20, y=30, width=100, height=40)],
    )
    return browser, page


def test_snapshot_exposes_coordinates_and_filters_covered_targets():
    parsed = _parse_elements(
        [{
            "index": 4,
            "tag": "button",
            "role": "button",
            "text": "Add to cart",
            "attributes": {},
            "bounds": {"x": 10, "y": 20, "width": 80, "height": 30},
        }]
    )
    assert parsed[0].center == (50.0, 35.0)
    assert "@ (50,35)" in parsed[0].to_text()
    assert "elementFromPoint" in snapshot_module._BUILD_DOM_TREE_JS


def test_vault_fill_returns_only_success_and_uses_coordinates():
    browser, page = _browser()
    seen = []

    class Provider:
        def authorize_item(self, item_id, origin):
            assert (item_id, origin) == ("item-123", "https://shop.example")
            return True

        def resolve_item(self, item_id, context):
            seen.append((item_id, context.field_kind))
            return SecretMaterial("vault-password")

    register_vault_provider("test-vault", Provider(), replace=True)
    try:
        assert browser.fill_vault_item(
            3,
            "item-123",
            field_kind="password",
            provider="test-vault",
        ) is True
    finally:
        unregister_vault_provider("test-vault")
    assert seen == [("item-123", "password")]
    assert page.mouse.clicks == [(70.0, 50.0)]
    assert page.loc.filled == "vault-password"


def test_vault_provider_failure_does_not_expose_provider_text():
    browser, _ = _browser()

    class Provider:
        def authorize_item(self, _item_id, _origin):
            return True

        def resolve_item(self, _item_id, _context):
            raise RuntimeError("plaintext-vault-value")

    register_vault_provider("bad-vault", Provider(), replace=True)
    try:
        with pytest.raises(RuntimeError) as error:
            browser.fill_vault_item(3, "item-123", field_kind="password", provider="bad-vault")
    finally:
        unregister_vault_provider("bad-vault")
    assert "plaintext-vault-value" not in str(error.value)
    assert "plaintext-vault-value" not in repr(error.value.__context__)


def test_vault_provider_must_authorize_origin():
    browser, _ = _browser()

    class Provider:
        def authorize_item(self, _item_id, _origin):
            return False

        def resolve_item(self, _item_id, _context):
            raise AssertionError("must not resolve an unauthorized item")

    register_vault_provider("blocked-vault", Provider(), replace=True)
    try:
        with pytest.raises(Exception, match="not allowed"):
            browser.fill_vault_item(3, "item-123", field_kind="password", provider="blocked-vault")
    finally:
        unregister_vault_provider("blocked-vault")


def test_redaction_replaces_values_in_errors_and_text():
    assert redact_text("value=secret", ["secret"]) == "value=[REDACTED]"
    error = redacted_exception(RuntimeError("secret"), ["secret"])
    assert "secret" not in str(error)
    assert "[REDACTED]" in str(error)


def test_managed_flow_is_planned_by_default_without_running_commands():
    config = {
        "version": "2",
        "tailnet": {"command": ["tailscale", "up"]},
        "exit": {
            "command": ["curl", "-fsS", "https://example/exit"],
            "expected": "1.2.3.4",
            "identity_command": ["tailscale", "status", "--json"],
            "identity_expected": "rpi5",
        },
        "canary": {"command": ["vault-canary"]},
        "run": {"command": ["wraith", "agent", "https://shop.example"]},
    }
    called = []
    results = run_flow(config, runner=lambda *args, **kwargs: called.append(args))
    assert [result.status for result in results] == [
        "planned",
        "planned",
        "planned",
        "planned",
        "planned",
    ]
    assert called == []


def test_managed_flow_requires_apply_for_execute():
    config = {
        "version": "2",
        "tailnet": {},
        "exit": {
            "command": ["exit-check"],
            "expected": "home",
            "identity_command": ["tailscale", "status", "--json"],
            "identity_expected": "rpi5",
        },
        "canary": {},
        "run": {"command": ["purchase"]},
    }
    with pytest.raises(ManagedRunError, match="--execute requires --apply"):
        run_flow(config, execute=True)

    config["canary"] = {}
    with pytest.raises(ManagedRunError, match="vault canary"):
        run_flow(config, apply=True, execute=True)


def test_managed_timeout_does_not_retain_command_output():
    secret = "plaintext-vault-value"

    def runner(_command, **_kwargs):
        error = subprocess.TimeoutExpired(["canary"], 1, output=secret, stderr=secret)
        raise error

    config = {
        "version": "2",
        "tailnet": {},
        "exit": {
            "command": ["exit-check"],
            "expected": "home",
            "identity_command": ["tailscale", "status", "--json"],
            "identity_expected": "rpi5",
        },
        "canary": {"command": ["canary"]},
        "run": {},
    }
    with pytest.raises(ManagedRunError) as caught:
        run_flow(config, apply=True, runner=runner)
    assert secret not in repr(caught.value)
    assert secret not in repr(caught.value.__context__)


def test_managed_flow_never_returns_command_output():
    config = {
        "version": "2",
        "tailnet": {},
        "exit": {
            "command": ["exit-check"],
            "expected": "home",
            "identity_command": ["tailscale", "status", "--json"],
            "identity_expected": "rpi5",
        },
        "canary": {"command": ["canary"]},
        "run": {"command": ["purchase"]},
    }

    def runner(command, **_kwargs):
        if command == ["exit-check"]:
            output = "home"
        elif command == ["tailscale", "status", "--json"]:
            output = json.dumps({
                "BackendState": "Running",
                "ExitNodeStatus": {"Online": True, "HostName": "rpi5"},
            })
        else:
            output = "plaintext-vault-value"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    results = run_flow(config, apply=True, runner=runner)
    assert [result.name for result in results] == ["exit", "exit_identity", "canary", "run"]
    assert all("plaintext-vault-value" not in repr(result) for result in results)


def test_managed_flow_rejects_missing_identity_check():
    config = {
        "version": "2",
        "tailnet": {},
        "exit": {"command": ["exit-check"], "expected": "home"},
        "canary": {"command": ["canary"]},
        "run": {},
    }
    with pytest.raises(ManagedRunError, match="identity_command"):
        run_flow(config, apply=True)


@pytest.mark.parametrize(
    "identity",
    [
        {"BackendState": "Stopped", "ExitNodeStatus": {"Online": True, "HostName": "rpi5"}},
        {"BackendState": "Running", "ExitNodeStatus": {"Online": True, "HostName": "other"}},
    ],
)
def test_managed_flow_requires_healthy_expected_exit_node(identity):
    config = {
        "version": "2",
        "tailnet": {},
        "exit": {
            "command": ["exit-check"],
            "expected": "home",
            "identity_command": ["tailscale", "status", "--json"],
            "identity_expected": "rpi5",
        },
        "canary": {"command": ["canary"]},
        "run": {},
    }

    def runner(command, **_kwargs):
        if command == ["exit-check"]:
            return subprocess.CompletedProcess(command, 0, stdout="home", stderr="")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(identity),
            stderr="network is down",
        )

    with pytest.raises(ManagedRunError, match="identity or health"):
        run_flow(config, apply=True, runner=runner)
