"""Offline tests for the CLI's ``--proxy`` / ``--dataimpulse`` / ``--anyip`` resolution.

No browser, no network: exercises ``wraith.cli._resolve_proxy`` over parsed
argparse namespaces with provider credentials injected via the environment.
"""

from __future__ import annotations

import argparse

import pytest

from wraith import cli


def _ns(**kw) -> argparse.Namespace:
    base = dict(proxy=None, dataimpulse=False, anyip=False, proxy_country=None, proxy_network=None)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def anyip_env(monkeypatch):
    monkeypatch.setenv("ANYIP_USERNAME", "ab12")
    monkeypatch.setenv("ANYIP_PASSWORD", "pw")


@pytest.fixture
def no_creds(monkeypatch, tmp_path):
    for k in ("ANYIP_USERNAME", "ANYIP_PASSWORD", "DATAIMPULSE_USERNAME", "DATAIMPULSE_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # so ~/.secrets resolves to an empty dir


def test_plain_url_passes_through():
    assert cli._resolve_proxy(_ns(proxy="http://u:p@h:1")) == "http://u:p@h:1"
    assert cli._resolve_proxy(_ns()) is None


def test_anyip_flag_builds_rotating_exit(anyip_env, capsys):
    url = cli._resolve_proxy(_ns(anyip=True, proxy_country="il", proxy_network="mobile"))
    assert url == "http://user_ab12,type_mobile,country_IL:pw@portal.anyip.io:1080"
    err = capsys.readouterr().err
    assert "anyIP" in err and "country=il" in err and "network=mobile" in err


def test_proxy_literal_anyip_is_case_insensitive(anyip_env):
    assert cli._resolve_proxy(_ns(proxy=" AnyIP ")) == "http://user_ab12:pw@portal.anyip.io:1080"


def test_anyip_missing_creds_is_clean_exit(no_creds):
    with pytest.raises(SystemExit) as ei:
        cli._resolve_proxy(_ns(anyip=True))
    assert "ANYIP_USERNAME" in str(ei.value)


def test_both_providers_is_an_error(anyip_env):
    with pytest.raises(SystemExit, match="choose one"):
        cli._resolve_proxy(_ns(anyip=True, dataimpulse=True))


def test_dataimpulse_still_works_and_ignores_network(monkeypatch, capsys):
    monkeypatch.setenv("DATAIMPULSE_USERNAME", "acct")
    monkeypatch.setenv("DATAIMPULSE_PASSWORD", "pw")
    url = cli._resolve_proxy(_ns(dataimpulse=True, proxy_country="il", proxy_network="mobile"))
    assert url == "http://acct__cr.il:pw@gw.dataimpulse.com:823"
    assert "anyIP-only" in capsys.readouterr().err


def test_parser_exposes_flags_on_launch(anyip_env):
    ns = cli.build_parser().parse_args(
        ["launch", "https://example.com", "--anyip", "--proxy-country", "us", "--proxy-network", "residential"]
    )
    assert ns.anyip is True
    assert cli._resolve_proxy(ns) == "http://user_ab12,type_residential,country_US:pw@portal.anyip.io:1080"


def test_parser_rejects_unknown_network():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["launch", "https://example.com", "--proxy-network", "datacenter"])
