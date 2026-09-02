# Changelog

All notable changes to **Wraith** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Profile sync** — move a domain-scoped login from a laptop to a remote
  Wraith, the way Browser Use's "sync your local cookies to cloud" works, but
  end-to-end encrypted with no account and no inbound port.
  - `wraith.chrome`: opt-in decryptor for Chrome/Chromium cookies across macOS
    (Keychain), Linux (Secret Service / `peanuts`), and Windows (DPAPI); refuses
    app-bound `v20` values with guidance to use `--from login`.
  - `wraith.deaddrop`: an anonymous, login-free transport. One ephemeral secret
    per transfer derives an unguessable relay slot and a ChaCha20-Poly1305 key;
    the sealed blob is size-padded, slot-bound, and freshness-gated. A dumb
    Cloudflare Worker relay (`deploy/worker.js`) stores one ciphertext per slot
    for ~10 minutes and hands it over exactly once.
  - `wraith.profile` + `wraith profile sync` / `wraith profile receive` CLI: pick
    a source (`chrome`/`firefox`/`zen`/`login`), scope to a domain, print a
    one-shot pairing code.
  - `receive_profile` MCP tool: the remote pulls the jar and injects it — a
    cross-machine identity borrow, no password ever seen by the agent.

## [0.2.0] - 2026-09-01

### Added

- Added opaque secret capabilities for browser field fills.
- Added process-local provider registration for library and embedded MCP use.
- Added exact origin, field kind, expiry, and use-count checks.
- Added session taint after a successful secret fill.
- Blocked storage-state export and screenshots from a tainted session by default.
- Removed editable `value` attributes from agent snapshots.
- Added `fill_secret` to the MCP browser tools.

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

[Unreleased]: https://github.com/YogevKr/wraith/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/YogevKr/wraith/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/YogevKr/wraith/releases/tag/v0.1.0
