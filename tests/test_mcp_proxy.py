"""Offline tests: the MCP server's browser honours a configured / env proxy.

Gap this closes: ``wraith mcp`` built its ``AgentBrowser`` with no launch
options, so neither provider (nor any proxy) could reach the browser that
Claude Code / Claude Desktop drive over MCP. Now ``configure(proxy=...)`` (set
by the CLI's shared proxy flags) or ``WRAITH_PROXY`` / ``WRAITH_PROXY_COUNTRY``
/ ``WRAITH_PROXY_NETWORK`` env feed ``AgentBrowser(**launch_kw)``. The
AgentBrowser is faked — no browser, no network.
"""

from __future__ import annotations

import pytest

import wraith.agent as agent_mod
import wraith.mcp as m
from wraith import cli


class FakeBrowser:
    instances: list["FakeBrowser"] = []

    def __init__(self, reputation=None, **launch_kw):
        self.reputation = reputation
        self.launch_kw = launch_kw
        FakeBrowser.instances.append(self)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_mod, "AgentBrowser", FakeBrowser)
    FakeBrowser.instances.clear()
    m._reset_browser()
    m._LAUNCH_KW = None
    for k in (m.ENV_PROXY, m.ENV_PROXY_COUNTRY, m.ENV_PROXY_NETWORK,
              "ANYIP_USERNAME", "ANYIP_PASSWORD", "DATAIMPULSE_USERNAME", "DATAIMPULSE_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield
    m._reset_browser()
    m._LAUNCH_KW = None


def test_default_is_no_proxy():
    b = m._get_browser()
    assert b.launch_kw == {}


def test_configure_proxy_reaches_agent_browser():
    m.configure(proxy="http://u:p@h:1")
    assert m._get_browser().launch_kw == {"proxy": "http://u:p@h:1"}


def test_configure_none_drops_key_and_beats_env(monkeypatch):
    monkeypatch.setenv(m.ENV_PROXY, "http://env:p@h:1")
    m.configure(proxy=None)
    assert m._get_browser().launch_kw == {}


def test_env_url_passthrough(monkeypatch):
    monkeypatch.setenv(m.ENV_PROXY, "socks5://h:1080")
    assert m._get_browser().launch_kw == {"proxy": "socks5://h:1080"}


def test_env_provider_literal_with_country_and_network(monkeypatch, capsys):
    monkeypatch.setenv("ANYIP_USERNAME", "ab12")
    monkeypatch.setenv("ANYIP_PASSWORD", "pw")
    monkeypatch.setenv(m.ENV_PROXY, "anyip")
    monkeypatch.setenv(m.ENV_PROXY_COUNTRY, "il")
    monkeypatch.setenv(m.ENV_PROXY_NETWORK, "mobile")
    b = m._get_browser()
    assert b.launch_kw == {"proxy": "http://user_ab12,type_mobile,country_IL:pw@portal.anyip.io:1080"}
    assert "anyIP" in capsys.readouterr().err


def test_env_dataimpulse_literal(monkeypatch):
    monkeypatch.setenv("DATAIMPULSE_USERNAME", "acct")
    monkeypatch.setenv("DATAIMPULSE_PASSWORD", "pw")
    monkeypatch.setenv(m.ENV_PROXY, "dataimpulse")
    monkeypatch.setenv(m.ENV_PROXY_COUNTRY, "il")
    assert m._get_browser().launch_kw == {"proxy": "http://acct__cr.il:pw@gw.dataimpulse.com:823"}


def test_env_provider_without_creds_degrades_to_no_proxy(monkeypatch, capsys):
    monkeypatch.setenv(m.ENV_PROXY, "anyip")
    b = m._get_browser()
    assert b.launch_kw == {}
    err = capsys.readouterr().err
    assert "ignored" in err and "ANYIP_USERNAME" in err


def test_cli_mcp_flags_configure_server(monkeypatch):
    monkeypatch.setenv("ANYIP_USERNAME", "ab12")
    monkeypatch.setenv("ANYIP_PASSWORD", "pw")
    ns = cli.build_parser().parse_args(["mcp", "--anyip", "--proxy-country", "us"])
    assert cli._proxy_flags_given(ns)
    # Drive the same path cmd_mcp takes, minus app.run().
    m.configure(proxy=cli._resolve_proxy(ns))
    assert m._get_browser().launch_kw == {"proxy": "http://user_ab12,country_US:pw@portal.anyip.io:1080"}


def test_cli_mcp_without_flags_leaves_env_fallback():
    ns = cli.build_parser().parse_args(["mcp"])
    assert not cli._proxy_flags_given(ns)
