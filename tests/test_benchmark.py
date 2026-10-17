"""Offline tests for the network fingerprint checks and the stealth benchmark."""

from __future__ import annotations

import json

import pytest

from wraith import benchmark, cli, detect

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
FIREFOX_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) Gecko/20100101 Firefox/152.0"
CHROME_H2 = "1:65536;2:0;4:6291456;6:262144|15663105|0|m,a,s,p"
FIREFOX_H2 = "1:65536;2:0;4:131072;5:16384|12517377|0|m,p,a,s"


def _peet(ua, akamai, ja4="t13d1517h2_8daaf6152771_cb7bf5808d99", version="h2"):
    return {
        "user_agent": ua,
        "http_version": version,
        "tls": {"ja4": ja4},
        "http2": {"akamai_fingerprint": akamai},
    }


@pytest.mark.parametrize(
    "data, expected",
    [
        (
            _peet(CHROME_UA, CHROME_H2),
            dict.fromkeys(
                ["http2", "tls13", "headerUserAgent", "h2HeaderOrder", "h2Settings"],
                "pass",
            ),
        ),
        (
            _peet(FIREFOX_UA, FIREFOX_H2),
            {"h2HeaderOrder": "pass", "h2Settings": "pass"},
        ),
        (_peet(CHROME_UA, FIREFOX_H2), {"h2HeaderOrder": "fail", "h2Settings": "warn"}),
        (
            _peet(CHROME_UA, CHROME_H2.replace("6291456", "6291457")),
            {"h2HeaderOrder": "pass", "h2Settings": "warn"},
        ),
        (
            _peet(CHROME_UA.replace("Chrome/", "HeadlessChrome/"), CHROME_H2),
            {"headerUserAgent": "fail"},
        ),
        (
            _peet(CHROME_UA, "", ja4="t12d1516h1", version="HTTP/1.1"),
            {"http2": "fail", "tls13": "fail", "h2HeaderOrder": "unknown"},
        ),
        (
            _peet("curl/8.0", CHROME_H2),
            {"h2HeaderOrder": "unknown", "h2Settings": "unknown"},
        ),
    ],
)
def test_network_checks(data, expected):
    result = detect.network_checks(data)
    assert {k: result["checks"][k] for k in expected} == expected


def test_browser_family():
    assert detect.browser_family(CHROME_UA) == "chrome"
    assert detect.browser_family(FIREFOX_UA) == "firefox"
    assert (
        detect.browser_family("Mozilla/5.0 (Macintosh) Version/18.0 Safari/605.1.15")
        == "safari"
    )
    assert detect.browser_family(None) is None


def _record(runner, checks, details=None):
    return {"runner": runner, "checks": checks, "details": details or {}}


def test_regressions_only_count_a_pass_that_got_worse():
    previous = [
        _record("mac-camoufox", {"a": "pass", "b": "pass", "c": "warn", "d": "pass"})
    ]
    current = [
        _record(
            "mac-camoufox",
            {"a": "fail", "b": "unknown", "c": "fail", "d": "warn", "e": "fail"},
        ),
        _record("linux-chromium", {"a": "fail"}),  # no previous record
    ]
    assert benchmark.regressions(previous, current) == [
        "mac-camoufox: `a` pass → fail",
        "mac-camoufox: `d` pass → warn",
    ]


def test_fingerprint_changes_are_reported():
    previous = [_record("mac", {}, {"net": {"ja4": "t13_old", "akamai": "x"}})]
    current = [
        _record(
            "mac", {}, {"net": {"ja4": "t13_new", "akamai": "x"}, "user_agent": "UA"}
        )
    ]
    assert benchmark.fingerprint_changes(previous, current) == [
        "mac: `net.ja4` t13_old → t13_new"
    ]


def test_render_and_load(tmp_path):
    nested = tmp_path / "stealth-mac" / "results"
    nested.mkdir(parents=True)
    (nested / "a.json").write_text(
        json.dumps(_record("mac", {"bot.x": "pass"}, {"net_error": "boom"}))
    )
    records = benchmark.load_records(tmp_path)
    table = benchmark.render(records + [_record("linux", {"bot.y": "fail"})])

    assert "| Check | linux | mac |" in table
    assert "| `bot.x` | — | pass |" in table
    assert "- mac `net_error`: boom" in table
    assert benchmark.load_records(tmp_path / "missing") == []


def test_run_collects_each_part_and_notes_errors(monkeypatch):
    class _Page:
        def evaluate(self, js):
            return FIREFOX_UA

    class _Session:
        page = _Page()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("wraith.engine.launch", lambda **kw: _Session())
    monkeypatch.setattr(
        detect, "selftest", lambda page: {"checks": {"viewport": {"status": "pass"}}}
    )

    def unreachable(page):
        raise TimeoutError("tls.peet.ws did not load")

    monkeypatch.setattr(detect, "network_fingerprint", unreachable)
    monkeypatch.setattr(benchmark, "_fast_path_checks", lambda detect: None)
    replays = []
    monkeypatch.setattr(
        benchmark, "_replay_checks", lambda detect, net: replays.append(net)
    )

    record = benchmark.run("camoufox", runner="mac-camoufox-headless")
    assert replays == [None]  # no browser fingerprint to compare with

    assert record["checks"] == {"bot.viewport": "pass"}
    assert record["details"]["net_error"] == "TimeoutError: tls.peet.ws did not load"
    assert record["details"]["user_agent"] == FIREFOX_UA
    assert benchmark.runner_name("chromium", False).endswith("-chromium-headed")


@pytest.mark.parametrize(
    "ja4, expected", [("t13d1617h2_a_b", "pass"), ("t13d1717h2_c_b", "fail")]
)
def test_replay_check_compares_with_the_browser(monkeypatch, ja4, expected):
    sent = {}

    class _Response:
        def json(self):
            return _peet(FIREFOX_UA, FIREFOX_H2, ja4=ja4)

    def fake_fetch(url, headers=None):
        sent.update(headers)
        return _Response()

    monkeypatch.setattr("wraith.fastpath.fetch", fake_fetch)
    browser = {"user_agent": FIREFOX_UA, "ja4": "t13d1617h2_a_b", "akamai": FIREFOX_H2}
    result = benchmark._replay_checks(detect, browser)

    assert result["checks"] == {"matchesBrowser": expected}
    assert sent == {"User-Agent": FIREFOX_UA}
    assert (
        benchmark._replay_checks(detect, None) is None
    )  # the browser was not measured


def test_cli_run_and_compare(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        benchmark,
        "run",
        lambda engine, headless, runner: _record(runner or engine, {"bot.x": "fail"}),
    )
    out = tmp_path / "current" / "mac.json"
    assert (
        cli.main(
            [
                "benchmark",
                "run",
                "--engine",
                "chromium",
                "--runner",
                "mac",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert json.loads(out.read_text())["runner"] == "mac"

    previous = tmp_path / "previous"
    previous.mkdir()
    issue, summary = tmp_path / "issue.md", tmp_path / "summary.md"
    args = [
        "benchmark",
        "compare",
        str(out.parent),
        "--previous",
        str(previous),
        "--summary",
        str(summary),
        "--issue",
        str(issue),
    ]
    cli.main(args)
    assert (
        summary.exists() and not issue.exists()
    )  # no previous record: nothing regressed

    (previous / "mac.json").write_text(json.dumps(_record("mac", {"bot.x": "pass"})))
    cli.main(args)
    assert "mac: `bot.x` pass → fail" in issue.read_text()


class _NotesPage:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def evaluate(self, js, names):
        self.calls.append(names)
        if self.fail:
            raise RuntimeError("page closed")
        return {name: f"🟡 {name} 1 ms reason for {name}" for name in names}


def test_bot_checks_keep_the_detectors_reason_for_warn_and_fail(monkeypatch):
    statuses = {
        "headless": "warn",
        "viewport": "pass",
        "useragent": "fail",
        "dummyFn": "unknown",
    }
    result = {"checks": {name: {"status": status} for name, status in statuses.items()}}
    monkeypatch.setattr(detect, "selftest", lambda page: result)

    page = _NotesPage()
    bot = benchmark._bot_checks(detect, page)
    assert page.calls == [["headless", "useragent"]]
    assert bot["notes"]["headless"] == "🟡 headless 1 ms reason for headless"

    table = benchmark.render([_record("linux", {"bot.headless": "warn"}, {"bot": bot})])
    assert "- linux `bot.headless`: 🟡 headless 1 ms reason for headless" in table

    failing = benchmark._bot_checks(detect, _NotesPage(fail=True))
    assert failing["checks"] == statuses  # a missing note never loses a status
    assert failing["notes"]["useragent"] == "no note (RuntimeError)"
    assert detect.bot_detector_notes(page, []) == {} and len(page.calls) == 1
