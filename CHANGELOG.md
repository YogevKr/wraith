# Changelog

All notable changes to **Wraith** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **`providers.AnyIP`**: first-class [anyIP](https://anyip.io) residential +
  mobile provider alongside `DataImpulse`. Same contract — `rotating()`,
  `sticky(session_id, minutes=, replace=, same_asn=)`, `pool(n)` → `ProxyPool`,
  lazy `AnyIPAuthError`, creds from `ANYIP_USERNAME`/`ANYIP_PASSWORD` env or
  `~/.secrets`. Emits anyIP's `user_<id>,type_…,country_…,region_…,city_…,asn_…,
  pool_…,session_…,sesstime_…` username flags (uppercase country, lowercase
  slugs) and validates the city→region→country chain, session-id charset/length
  and `sesstime` bounds locally instead of surfacing them as gateway 407s.
- **`proxy.to_playwright_proxy()`**: converts a proxy URL string (or bare
  `host:port`) into Playwright's `{"server", "username", "password"}` dict;
  passes mappings/`None` through.
- **CLI**: `--anyip` / `--proxy anyip` shortcut and `--proxy-network
  residential|mobile` (anyIP only) on every command that takes `--proxy`;
  `--proxy-country` now steers both providers.
- **MCP proxy support**: `wraith mcp` now takes the shared proxy flags
  (`--proxy` / `--dataimpulse` / `--anyip` / `--proxy-country` /
  `--proxy-network` / `--proxy-*-cmd`) and, when none is given, reads
  `WRAITH_PROXY` (URL or `dataimpulse` / `anyip`), `WRAITH_PROXY_COUNTRY` and
  `WRAITH_PROXY_NETWORK` from the environment; the server's `AgentBrowser` is
  launched with the resulting `proxy=`. Previously the MCP browser could not
  use any proxy. A failing env spec is logged and the browser starts
  un-proxied instead of the server never coming up. New
  `wraith.mcp.configure(**launch_kw)` and
  `wraith.providers.resolve_proxy_spec()` (the one CLI/MCP resolution path).
- **`wraith.credentials`**: every secret can now be supplied by a **command**.
  `resolve_secret(name, value=, command=)` resolves explicit value → explicit
  command → env `NAME` → env `NAME_CMD` (run) → `~/.secrets` `NAME=` →
  `~/.secrets` `NAME_CMD=` (run); commands run via the shell with stdin closed
  and a timeout, trailing newlines stripped, and a failing/empty command raises
  `SecretCommandError` (which names the command, never its output). Wired into
  `DataImpulse` / `AnyIP` (`username_cmd=` / `password_cmd=` kwargs,
  `DATAIMPULSE_*_CMD` / `ANYIP_*_CMD` env), the solver adapters (`CapSolver` /
  `TwoCaptcha` now default to `CAPSOLVER_API_KEY` / `TWOCAPTCHA_API_KEY` and
  their `_CMD` variants, plus an `api_key_cmd=` kwarg), and the CLI
  (`--proxy-username-cmd` / `--proxy-password-cmd`).

### Fixed
- **`engine.launch(proxy=<url string>)`** never reached the browser: the string
  was forwarded raw and Camoufox failed with `'str' object has no attribute
  'get'` (Playwright likewise requires a `ProxySettings` dict). `launch` now
  runs every `proxy=` kwarg through `to_playwright_proxy()`, so the URL strings
  `ProxyPool`, the provider classes, and `clear_challenge`'s pool rotation emit
  work end to end. Dicts are passed through unchanged.
- **`agent.py`**: `AgentBrowser.type()`'s `clear=True` path no longer trusts
  "no exception" as proof a field was cleared. `locator.fill("")` silently
  no-ops against editors whose real document lives in a JS-owned model
  decoupled from the DOM node's raw value (found live against a
  CodeMirror-backed JSON editor: the field read as cleared, but nothing had
  actually changed, so the new text was typed on top of the untouched
  original document instead of replacing it — a ~76-line document doubled to
  ~151 lines). `type()` now reads the field back after `fill("")`, and if it
  is still non-empty, falls back to a keyboard-driven select-all + delete
  (`focus()` then `ControlOrMeta+a`, `Backspace`, `Delete` — deliberately not
  `click()`, since a virtualized editor's overlay can intercept pointer
  events and time out a click on an otherwise-focusable element). If the
  field still can't be verified empty after both attempts, `type()` now
  raises the new `ClearFailedError` instead of silently typing on unknown
  existing content.

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
