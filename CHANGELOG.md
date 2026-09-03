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

## [0.4.0] - 2026-09-02

### Changed

- Support **mcp 2.x**. The SDK renamed `FastMCP` (`mcp.server.fastmcp`) to
  `MCPServer` (`mcp.server.mcpserver`); `wraith.mcp` now runs on both mcp 1.x and
  2.x through a small import shim (the constructor, `@app.tool()`, and
  `app.run()` are call-compatible). The dependency pin widened from `<2` to
  `<3`, and the lock now resolves mcp 2.1.1 (pulling its new transitive deps:
  `httpx2`, `mcp-types`, `opentelemetry-api`, `truststore`).

## [0.3.2] - 2026-09-02

### Fixed

- Pin `mcp>=1,<2`. `wraith.mcp` uses the v1 FastMCP API, which `mcp` 2.x renamed
  to `MCPServer`; an unpinned `pip install wraith` pulled 2.x and broke the
  import. (The lockfile already pinned v1, so CI/uv were unaffected — a plain
  `pip install` was not.)

## [0.3.1] - 2026-09-02

### Added

- **Revoke a drop** — `wraith profile revoke <code>` (and `deaddrop.burn`) delete
  a sealed blob at the relay without reading it. Whoever holds the secret can
  burn a drop they mis-sent or whose code leaked. It grants no new power (a GET
  already destroys on read); it just makes the cancel explicit.
- `wraith profile receive --out <file>` saves the pulled jar as Playwright
  storageState JSON. `receive` now requires an action (`--open` or `--out`) and
  refuses **before** the network call, so a one-shot drop is never consumed with
  nowhere to put the jar.

### Changed

- The relay is now a **SQLite-backed Durable Object** (one per slot) instead of
  KV, so read-and-delete is atomic — two racing pickups of a leaked code can no
  longer both read the blob. Deploys on the Workers Free plan; no KV namespace.
- PUT is idempotent (a retry re-sending the identical sealed bytes succeeds
  instead of a first-writer-wins collision); a consuming GET is no longer
  auto-retried (a retry after a lost response would lose the jar).

### Fixed

- `find_chrome_profile()` now detects the modern `Default/Network/Cookies`
  layout, so Chrome auto-detection works without `--profile`.
- Chrome cookie decryption resolves `Local State` from the real profile
  directory under the `Network/` layout (Windows decryption no longer fails).
- An all-app-bound (`v20`) Chrome store now surfaces the `--from login` guidance
  (new `AppBoundCookieError`) instead of a misleading "no cookies" result.
- `profile sync` reports Chrome keychain / app-bound failures as clean CLI
  errors instead of a traceback (`ChromeCookieError` derives from
  `NotImplementedError`, which the handler now catches).

## [0.3.0] - 2026-09-02

### Added

- **Profile sync** — move a domain-scoped login from a laptop to a remote
  Wraith, the way Browser Use's "sync your local cookies to cloud" works, but
  end-to-end encrypted with no account and no inbound port.
  - `wraith.chrome`: opt-in decryptor for Chrome/Chromium cookies across macOS
    (Keychain), Linux (Secret Service / `peanuts`), and Windows (DPAPI); refuses
    app-bound `v20` values with guidance to use `--from login`.
  - `wraith.deaddrop`: an anonymous, login-free transport. One ephemeral secret
    per transfer derives an unguessable relay slot and a ChaCha20-Poly1305 key;
    the sealed blob is size-padded, slot-bound, and freshness-gated. The relay
    client retries transient failures (timeouts, 429, 5xx) with backoff and
    guards the relay's body cap (`DropTooLarge`). A dumb Cloudflare Worker relay
    (`deploy/worker.js`) stores one ciphertext per slot for ~10 minutes, hands it
    over exactly once, and rate-limits per IP (`DROP_LIMITER`, 120/60s) to stop
    storage-abuse floods.
  - `wraith.profile` + `wraith profile sync` / `wraith profile receive` CLI: pick
    a source (`chrome`/`firefox`/`zen`/`login`), scope to a domain, print a
    one-shot pairing code. Clean, actionable errors for spent/expired/oversize
    drops.
  - `receive_profile` MCP tool: the remote pulls the jar and injects it — a
    cross-machine identity borrow, no password ever seen by the agent.
- Declared `cryptography` as a direct dependency (Chrome decryption + dead-drop
  AEAD/HKDF); it was only a transitive `pyjwt[crypto]` extra before.

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

[Unreleased]: https://github.com/YogevKr/wraith/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/YogevKr/wraith/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/YogevKr/wraith/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/YogevKr/wraith/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/YogevKr/wraith/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/YogevKr/wraith/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/YogevKr/wraith/releases/tag/v0.1.0
