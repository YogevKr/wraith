# Changelog

All notable changes to **Wraith** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-06-20

Initial public release — an identity-borrowing, MCP-native stealth browser for
autonomous agents.

### Added
- **Stealth engine** (`engine.py`): Camoufox (Firefox) primary + patchright-Chromium
  fallback; `viewport=None`, `geoip`, and a `playwright==1.55` compatibility guard.
- **`clear_challenge()`**: passes WAAP JS challenges, gates success on per-vendor
  clearance-cookie *validity* (incl. Akamai `_abck` `~0~`/`~-1~`), a best-effort
  behavioral nudge, and `proxy_pool` auto-rotation on `474`/`481`/`492`. Errors:
  `WaapRateLimitedError`, `WaapHardBlockError`, `WaapChallengeTimeout`.
- **Multi-vendor WAAP detection** (`detect.identify_waap` / `fingerprint`):
  Cloudflare, Akamai, DataDome, PerimeterX/HUMAN, Kasada, Imperva/Incapsula,
  Reblaze/Link11, AWS WAF, F5/Shape, reCAPTCHA, hCaptcha, SiteMinder.
- **Identity borrowing** (`identity.py`): extract & inject cookies from real
  Firefox/Zen profiles — the core move against reputation-based defenses.
- **Session harvesting** (`harvest.py`): capture a reusable `Authorization`+`Cookie`.
- **Proxies**: `ProxyPool` (rotation) + **DataImpulse** residential provider
  (rotating / sticky / `.pool(n)`).
- **Agent layer** (`agent.py` / `snapshot.py`): browser-use-style indexed
  `snapshot()` + index actions (`click` / `type` / `scroll` / `read`), and an
  auto-resilient `navigate()`.
- **MCP server** (`wraith mcp`): 9 tools (`navigate`, `snapshot`, `click`,
  `type_text`, `scroll`, `read`, `screenshot`, `detect_waap`, `borrow`).
- **reCAPTCHA** (`recaptcha.py`): `recaptcha_v3_score`, `harvest_token`, and
  pluggable solver-service adapters (`CapSolver`, `TwoCaptcha`).
- **CLI**: `launch | borrow | harvest | score | detect | agent | mcp`.
- **Docs**: `DETECTION.md` (vendor taxonomy + coverage matrix), `PLAYBOOK.md`
  (tier strategy, proxy rotation), `AGENTS.md` (agent API + MCP setup).

[0.1.0]: https://github.com/YogevKr/wraith/releases/tag/v0.1.0
