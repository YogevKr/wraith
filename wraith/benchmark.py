"""Stealth benchmark: one record per runner, compared with the previous run.

A *runner* is one OS + engine + mode, such as ``macos-camoufox-headless``.
:func:`run` launches the engine and collects:

* ``bot.*`` — rebrowser bot-detector checks (:func:`wraith.detect.selftest`),
* ``net.*`` — the browser's TLS and HTTP/2 fingerprint against the browser
  its User-Agent claims (:func:`wraith.detect.network_fingerprint`),
* ``fastpath.*`` — the same for the no-browser fast path, when ``curl_cffi``
  is installed.

Each check is ``pass``, ``warn``, ``fail``, or ``unknown`` (the page could not
be read, so nothing was measured). :func:`regressions` lists checks that
passed in the previous record of the same runner and now warn or fail; an
``unknown`` result is never a regression. The weekly workflow
(``.github/workflows/stealth-benchmark.yml``) opens an issue for them.
"""

from __future__ import annotations

import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = [
    "run",
    "regressions",
    "fingerprint_changes",
    "render",
    "load_records",
    "runner_name",
]

_BAD = ("fail", "warn")


def runner_name(engine: str, headless: bool) -> str:
    """``<os>-<engine>-<headless|headed>``, the key that records are compared by."""
    system = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    return f"{system}-{engine}-{'headless' if headless else 'headed'}"


def run(engine: str, *, headless: bool = True, runner: Optional[str] = None) -> dict:
    """Launch ``engine`` and return one benchmark record."""
    from . import detect
    from .engine import launch

    record: dict[str, Any] = {
        "runner": runner or runner_name(engine, headless),
        "engine": engine,
        "headless": headless,
        "platform": platform.platform(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checks": {},
        "details": {},
    }
    with launch(engine=engine, headless=headless, geoip=False) as session:
        record["details"]["user_agent"] = session.page.evaluate(
            "() => navigator.userAgent"
        )
        _collect(record, "bot", lambda: _bot_checks(detect.selftest(session.page)))
        _collect(record, "net", lambda: detect.network_fingerprint(session.page))
    _collect(record, "fastpath", lambda: _fast_path_checks(detect))
    return record


def _bot_checks(result: dict) -> dict:
    return {
        "checks": {name: check["status"] for name, check in result["checks"].items()}
    }


def _fast_path_checks(detect: Any) -> Optional[dict]:
    from . import fastpath

    try:
        response = fastpath.fetch(detect.TLS_FINGERPRINT_URL)
    except fastpath.FastPathUnavailableError:
        return None
    return detect.network_checks(response.json())


def _collect(record: dict, prefix: str, measure: Any) -> None:
    """Add ``prefix.*`` checks; an error leaves a note instead of checks."""
    try:
        result = measure()
    except Exception as exc:
        record["details"][f"{prefix}_error"] = f"{type(exc).__name__}: {exc}"[:300]
        return
    if result is None:
        return
    for name, status in result["checks"].items():
        record["checks"][f"{prefix}.{name}"] = status
    extra = {k: v for k, v in result.items() if k != "checks"}
    if extra:
        record["details"][prefix] = extra


def regressions(previous: Iterable[dict], current: Iterable[dict]) -> list[str]:
    """Checks that passed in the previous record of a runner and now warn or fail."""
    before = {record["runner"]: record.get("checks", {}) for record in previous}
    found = []
    for record in current:
        old = before.get(record["runner"], {})
        for name, status in sorted(record.get("checks", {}).items()):
            if old.get(name) == "pass" and status in _BAD:
                found.append(f"{record['runner']}: `{name}` pass → {status}")
    return found


def fingerprint_changes(previous: Iterable[dict], current: Iterable[dict]) -> list[str]:
    """JA4 or HTTP/2 fingerprints that differ from the previous record of a runner.

    A change is not a failure: a browser update changes them. It is the place
    to look when a site starts to block one engine.
    """
    before = {record["runner"]: record.get("details", {}) for record in previous}
    found = []
    for record in current:
        old = before.get(record["runner"], {})
        for source, new in sorted(record.get("details", {}).items()):
            if not isinstance(new, dict) or not isinstance(old.get(source), dict):
                continue
            for key in ("ja4", "akamai"):
                was, now = old[source].get(key), new.get(key)
                if was and now and was != now:
                    found.append(f"{record['runner']}: `{source}.{key}` {was} → {now}")
    return found


def render(records: Iterable[dict]) -> str:
    """A markdown table: one row per check, one column per runner."""
    records = sorted(records, key=lambda r: r["runner"])
    names = sorted({name for record in records for name in record.get("checks", {})})
    lines = [
        "| Check | " + " | ".join(r["runner"] for r in records) + " |",
        "|---|" + "---|" * len(records),
    ]
    for name in names:
        cells = [r.get("checks", {}).get(name, "—") for r in records]
        lines.append(f"| `{name}` | " + " | ".join(cells) + " |")
    for record in records:
        for key, note in sorted(record.get("details", {}).items()):
            if key.endswith("_error"):
                lines.append(f"\n- {record['runner']} `{key}`: {note}")
    return "\n".join(lines)


def load_records(folder: str | Path) -> list[dict]:
    """Every ``*.json`` record under ``folder`` (artifact downloads nest them)."""
    root = Path(folder)
    if not root.is_dir():
        return []
    return [json.loads(path.read_text()) for path in sorted(root.rglob("*.json"))]
