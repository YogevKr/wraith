# 🌀 Wraith

> A stealth, identity-borrowing, MCP-native agent browser.

[![CI](https://github.com/YogevKr/wraith/actions/workflows/ci.yml/badge.svg)](https://github.com/YogevKr/wraith/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

**Website:** [wraithbrowser.dev](https://wraithbrowser.dev) · the landing page lives in [`site/`](site/).

Wraith is a Python toolkit that gives an autonomous agent a real browser that is
hard to fingerprint and easy to drive. It pairs a hardened Firefox engine
(Camoufox) with **identity borrowing** — reusing a warmed, already-authenticated
session from one of your own real browser profiles — and exposes the whole thing
as both a CLI and an **MCP server** so an LLM can perceive and act on pages by
index.

---

## ⚖️ Responsible Use & Legal

Wraith is a **dual-use** tool. Like the established projects in its category
([Camoufox](https://camoufox.com/), [nodriver](https://github.com/ultrafunkamsterdam/nodriver),
[browser-use](https://github.com/browser-use/browser-use),
[undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver)),
it can be used well or badly. It is published for **legitimate** purposes:

- Accessing **your own** accounts and data, on your own machine, with your own
  warmed browser profile.
- **Authorized** security testing and bot-defense research (where you have
  permission to test the target).
- Personal automation, scraping of data you are entitled to, and reproducible
  research into anti-bot / WAAP systems.

**Please use it responsibly:**

- **Respect** each target site's Terms of Service and all applicable laws
  (e.g. CFAA and its equivalents).
- **Identity borrowing reads only your own local browser profiles.** Do not use
  it against accounts or sessions that are not yours.
- **Do not** use Wraith for fraud, account takeover, credential stuffing, mass
  abuse, spam, or to circumvent access controls you are not authorized to bypass.

You are responsible for how you use this software. If your use isn't clearly
covered by the legitimate cases above, don't do it. Wraith is released under the
[MIT License](LICENSE) **with no warranty**.

---

## Why identity borrowing (not "solving")

Modern defenses like **reCAPTCHA-v3** and **Reblaze/Link11 `ac_v2`** are
*reputation* systems, not puzzles. reCAPTCHA-v3 has **no client-side solver**: it
returns a `0.0..1.0` score derived from your Google-account cookies, aged
browsing history, and IP reputation. A fresh automated profile scores ~0.1–0.3
("bot") no matter how good the stealth engine is, because it has no history to
vouch for it. You cannot fake reputation.

So Wraith doesn't try. For **your own** sites and accounts, it **borrows a warmed
identity**: it reads your live session cookies straight out of your real Firefox
or Zen profile on disk (or harvests a live bearer token from network traffic) and
injects them into the stealth context. The agent then navigates as the
already-authenticated user — the reputation comes along for free.

For sites with a *solvable* JS interstitial (e.g. Reblaze `ac_v2`), no identity
is needed at all: a real Firefox engine clears the challenge natively, and
`clear_challenge()` just waits for the clearance cookie to appear.

## Features

- **Stealth-first engine** — Camoufox (hardened Firefox) primary, patchright
  Chromium fallback; enforces the `playwright==1.55` pin Camoufox needs.
- **Identity borrowing** — discover and read cookies from your own Firefox / Zen
  / Chrome profiles and inject them into the stealth context.
- **No-browser TLS fast path** — `wraith.fastpath` (curl_cffi) replays a
  borrowed/harvested session with a real-browser TLS+HTTP/2 fingerprint and **no
  browser**: do the expensive challenge-clear/login once in Camoufox, then drive
  authenticated requests cheaply. `wraith fetch` / MCP `fetch`.
  *(`pip install 'wraith[fastpath]'`.)*
- **Live session harvesting** — latch the first request carrying both an
  `Authorization` header and a named auth cookie, for tokens never stored on
  disk. Export a portable Playwright `storageState` from any cleared session.
- **WAAP/bot-defense fingerprinting** — `identify_waap()` recognises Reblaze/
  Link11, Akamai, reCAPTCHA, DataDome, Imperva/Incapsula, Kasada, SiteMinder;
  `classify_response()` / `is_blocked()` tell the fast path when to escalate.
- **Challenge solving** — a vendor-dispatching `Challenge` + `solve_challenge()`
  (Turnstile / hCaptcha / reCAPTCHA v2+v3 / FunCaptcha / AWS-WAF via CapSolver /
  2Captcha) and `inject_token()` to feed a solved token back into the page.
  `clear_challenge` fails fast on a hard block instead of burning the timeout.
- **Stealth self-test** — `wraith selftest` runs the rebrowser leak suite and
  exits non-zero on a critical automation leak (CI/regression gate).
- **Agent perception layer** — browser-use-style indexed DOM snapshots with
  **change-observation** (what each action did), **new-element marking**, and
  **signature self-heal** for stale indices; act by index (`click`, `type`,
  `scroll`, `read`) across multiple tabs.
- **Opaque secret fills** — registered providers resolve secret handles only
  after Wraith checks the origin, field kind, expiry, and use limit. Snapshot
  output omits editable `value` attributes.
- **MCP-native** — a stdio MCP server (`wraith-mcp`) exposing the agent browser
  as tools (per-call snapshot control, inline screenshots, tabs, `fetch`) for
  any MCP client.
- **Residential proxy support** — a `ProxyPool` health state machine (cooldown /
  backoff / half-open / dead / recovery) plus a first-class DataImpulse provider
  for rotating/sticky exits.
- **Human-like behavior helpers** — curved/eased mouse movement and per-key
  typing cadence.
- **Resilient import** — a missing optional browser dep never breaks
  `import wraith`; it's recorded in `wraith.missing_imports`.

## Install

Wraith uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync                            # core deps (camoufox, playwright==1.55, patchright, httpx, mcp)
uv run camoufox fetch              # fetch the Camoufox Firefox build (primary engine)
uv run patchright install chromium # (optional) fetch patched Chromium for the fallback engine
```

Or with pip:

```bash
pip install wraith
camoufox fetch                     # fetch the Camoufox Firefox build
patchright install chromium        # (optional) fallback engine
```

Verify the install:

```bash
uv run python -c "import wraith; print(wraith.__version__)"
uv run wraith --help
```

> ⚠️ Camoufox 0.4.x crashes on `playwright >= 1.60` (a Firefox `pageError`
> serialization bug). Wraith pins **`playwright == 1.55.x`** and detects a
> mismatch up front with an actionable error. patchright is independently
> versioned and unaffected.

## Quickstart

### (a) Library — `AgentBrowser`

The agent browser is the highest-level entry point: navigate, perceive an indexed
snapshot, and act on elements by index.

```python
from wraith import agent_browser

# Self-launches a stealth Camoufox session; closes it on exit.
with agent_browser(engine="camoufox", headless=True) as ab:
    snap = ab.navigate("https://example.com")
    print(snap.to_text())          # [12]<button role=button>Search</button> ...

    ab.type(3, "wraith", enter=True)  # type into element [3] and press Enter
    ab.click(12)                       # click element [12]
    print(ab.read())                   # current page as markdown
```

#### Opaque secret capabilities

Use `fill_secret()` when an agent must fill a secret field. Do not give the
agent the secret value. Give it an opaque capability from your secret broker.

The capability contains these fields:

| Field | Purpose |
| --- | --- |
| `provider` | Selects a registered provider. |
| `handle` | Identifies provider-owned secret material. Wraith redacts it from representations. |
| `allowed_origins` | Lists exact HTTP or HTTPS origins. Scheme, host, and non-default port must match. |
| `field_kind` | Limits the target field to `password`, `username`, `email`, `otp`, `card-number`, `card-expiry`, `card-cvc`, or `text`. |
| `expires_at` | Sets an optional ISO 8601 expiry with a time zone. |
| `max_uses` | Sets the browser-side use limit. The default is one. |
| `capability_id` | Carries an optional provider audit identifier. Wraith always counts uses by provider and handle. |

Secret targets must use main-frame `input` or `textarea` elements. Wraith does
not accept contenteditable targets because readable page text can expose them.
Username and OTP fields must declare `autocomplete="username"` or
`autocomplete="one-time-code"`.

Register the provider before the fill. A provider must return `SecretMaterial`.
It must authenticate and consume the handle. It must also enforce trusted
policy at the broker boundary.

```python
from wraith import SecretMaterial, agent_browser, register_secret_provider


class BrokerProvider:
    def resolve(self, capability, context):
        # broker.consume() must authenticate the opaque handle.
        # It must check context.origin and its own policy.
        value = broker.consume(capability.handle, context=context)
        return SecretMaterial(value)


register_secret_provider("broker", BrokerProvider())

with agent_browser(engine="camoufox", headless=True) as ab:
    ab.navigate("https://accounts.example.com/login")
    ab.fill_secret(7, {
        "provider": "broker",
        "handle": "opaque-capability-token",
        "allowed_origins": ["https://accounts.example.com"],
        "field_kind": "password",
        "expires_at": "2030-09-01T12:00:00Z",
        "max_uses": 1,
    })
```

You can also pass `secret_providers={"broker": BrokerProvider()}` to
`agent_browser()`. This keeps the provider on one browser instance.

A successful fill marks the session as secret-tainted. Wraith then blocks
`save_storage_state()` and `screenshot()` by default. Library callers can set
`allow_secret_tainted=True` to accept either risk.

For embedded MCP, register the provider in the MCP server process before
`app.run()`:

```python
from wraith import register_secret_provider
from wraith.mcp import app

register_secret_provider("broker", BrokerProvider())
app.run()
```

The MCP agent calls `fill_secret(index, capability)`. The tool accepts and
returns no secret value. The normal `wraith-mcp` command has no provider by
default. A provider registration in another process does not affect it.

Wraith has no Instinct Vault provider. Direct use needs an Instinct provider or
broker adapter that Wraith can reach. See [Security Policy](SECURITY.md#opaque-secret-capabilities)
for trust limits and observed Instinct behavior.

Lower-level: launch a session and borrow a warmed identity from your own profile.

```python
import wraith

with wraith.browser(engine="camoufox", geoip=True) as s:
    # Borrow your own warmed identity from a real Zen/Firefox profile on disk.
    profile = wraith.find_zen_profiles()[0]
    cookies = wraith.extract_cookies(profile, domain_filter="example.com")
    wraith.inject_cookies(s.context, cookies)

    s.page.goto("https://example.com")            # navigate as the logged-in user

    # Optionally harvest a live bearer token the app mints per session.
    h = wraith.SessionHarvester(target_url="api.example.com", auth_cookie="session")
    h.attach(s.context)
    s.page.goto("https://example.com/dashboard")
    h.wait(timeout=60)
    h.save_session("example.session.json")
```

`import wraith` is resilient: if an optional browser dependency is missing, the
affected symbols are omitted and `wraith.missing_imports` records why.

### (b) CLI — `wraith`

The `wraith` console script groups its subcommands below. The default engine is
`camoufox`; pass `--engine chromium` for the patchright fallback or
`--engine auto` to let Wraith choose.

```bash
# agent   — open a URL and print a browser-use-style indexed snapshot
uv run wraith agent https://example.com
uv run wraith agent https://example.com --json

# borrow  — inject your own warmed cookies, open the site as that logged-in user
uv run wraith borrow https://example.com --host example.com
uv run wraith borrow https://example.com --profile "~/Library/Application Support/zen/Profiles/xxxx.default"

# harvest — capture a live {Authorization, Cookie, User-Agent} session
uv run wraith harvest https://example.com --target api.example.com --cookie session -o example.session.json

# fetch   — no-browser TLS-impersonation replay of a harvested session (fast path)
uv run wraith fetch https://api.example.com/me --session example.session.json --show-body

# selftest — run the stealth leak suite; exit non-zero on a critical leak
uv run wraith selftest
uv run wraith selftest --json

# score   — read this identity's reCAPTCHA-v3 reputation (fresh -> ~0.1-0.3)
uv run wraith score
uv run wraith score --engine chromium --json

# detect  — fingerprint a URL's bot/WAAP defenses
uv run wraith detect https://example.com
uv run wraith detect https://example.com --json

# launch  — just open a stealth browser, held open (headed)
uv run wraith launch https://example.com
uv run wraith launch https://example.com --headless --no-wait

# profile — sync a local login to a remote Wraith over an encrypted dead-drop
uv run wraith profile sync --relay https://<name>.workers.dev --from chrome --domain elal.com
uv run wraith profile receive <pairing-code> --open https://www.elal.com/   # remote: inject + browse
uv run wraith profile receive <pairing-code> --out session.json             # remote: save the jar
uv run wraith profile revoke  <pairing-code>                                # burn a mis-sent drop

# mcp     — run the MCP server over stdio (see below)
uv run wraith mcp
```

> The base `borrow` / `extract_cookies` path reads **Firefox/Zen** profiles
> (plaintext cookie store, same engine family as Camoufox) and still raises
> `ChromeEncryptionError` for Chrome, on purpose. To use a **Chrome** login,
> reach for `wraith profile sync --from chrome` below — it opts into the OS
> keychain decryptor (`wraith.chrome`) explicitly.

#### Profile sync — a laptop login on a remote Wraith

`wraith profile sync` moves a **domain-scoped** login from your laptop to a
remote Wraith (a headless box, `tmm`, a cloud VM) over an **anonymous,
login-free, end-to-end-encrypted dead-drop**. No account, no inbound port, no
long-lived key — the agent never sees the jar.

```
LAPTOP                                   RELAY (Cloudflare Worker)        REMOTE (wraith-mcp)
  wraith profile sync --from chrome ─┐   stores 1 sealed blob per         receive_profile(code)
    read + decrypt cookies           │   random slot, ~10 min TTL,        ─ pulls + opens in RAM
    scope to --domain                ├──▶ hands it over exactly once ─────▶ injects into live context
    seal under an ephemeral secret   │   (sees only ciphertext + IPs)      navigate() as that user
    print a one-shot pairing code ───┘
```

- **Source** (`--from`): `chrome`, `firefox`, `zen`, or `login` (open a window,
  sign in by hand — works on any OS, needs no keychain).
- **Transport**: one ephemeral secret per transfer derives an unguessable relay
  slot and a `ChaCha20-Poly1305` key; the blob is size-padded and freshness-gated.
  Move the pairing code out of band (ssh, password manager, QR). The relay is a
  SQLite-backed Durable Object (atomic single pickup, Free-plan) deployed from
  [`deploy/`](deploy/) with `wrangler deploy` — no KV namespace.
- **Revoke**: holding the secret also lets you burn a drop —
  `wraith profile revoke <code>` deletes it at the relay if you mis-sent it or
  the code leaked.
- **Over Tailscale instead?** Skip the relay: `ssh <host> wraith profile receive <code>`.

A cookie jar is the session past 2FA — keep syncs domain-scoped, and see
[SECURITY.md](SECURITY.md#profile-sync-dead-drop) for the trust limits.

### (c) MCP server

Wraith ships a stdio MCP server (`wraith-mcp`) that exposes the agent browser as
tools — `navigate`, `snapshot`, `click`, `type_text`, `fill_secret`, `scroll`, `read`,
`screenshot`, `detect_waap`, `borrow`, and `receive_profile`. Wire it into an MCP
client such as Claude Code:

```bash
claude mcp add wraith -- uv run --directory /path/to/wraith wraith-mcp
```

(Equivalently, the server starts via `uv run wraith mcp` or `uv run wraith-mcp`.)

## Architecture

Wraith is a set of focused, mostly-independent modules under `wraith/`:

| Module | Responsibility |
| --- | --- |
| [`engine`](wraith/engine.py) | Stealth launcher & engine selection. `launch()` / `browser()` return a `Session` (`.page`, `.context`, `.browser`); `clear_challenge()` is the cookie-free WAAP front door. Camoufox primary, patchright Chromium fallback; enforces the `playwright==1.55` pin. |
| [`identity`](wraith/identity.py) | **Signature feature.** Discover Firefox/Zen/Chrome profiles, `extract_cookies()`, normalize them, and `inject_cookies()` into a context. Firefox/Zen plaintext; Chrome raises `ChromeEncryptionError` (opt into `chrome` to decrypt). |
| [`chrome`](wraith/chrome.py) | Opt-in Chrome/Chromium cookie decryptor for `profile sync`: recovers the `os_crypt` key (macOS Keychain, Linux Secret-Service/`peanuts`, Windows DPAPI), decrypts `v10`/`v11` values, strips the Chrome 130+ host hash, refuses app-bound `v20`. |
| [`deaddrop`](wraith/deaddrop.py) | Anonymous, login-free, end-to-end-encrypted transport. One ephemeral secret per transfer → an unguessable relay slot + `ChaCha20-Poly1305` key; `seal`/`open_sealed`, `push`/`pull`, and a dot-joined base64url pairing `code`. Relay: [`deploy/worker.js`](deploy/worker.js). |
| [`profile`](wraith/profile.py) | Ties it together: `gather_cookies` (chrome/firefox/zen) or `capture_login_jar` (manual sign-in) → domain-scoped jar → `sync_profile` (push) / `receive_profile` (pull + inject). |
| [`harvest`](wraith/harvest.py) | Live session capture. `SessionHarvester` latches the first request carrying an `Authorization` header + auth cookie; `harvest_session()` is the high-level helper. |
| [`detect`](wraith/detect.py) | Diagnostics: `identify_waap()` (vendor fingerprinting), `recaptcha_v3_score()` (reputation read), `bot_detector()` (rebrowser automation tells), `fingerprint()`. |
| [`behavior`](wraith/behavior.py) | Human-like helpers: `human_move()` (curved/eased/jittered mouse), `human_type()` (per-key cadence), `dwell()`. |
| [`agent`](wraith/agent.py) | The perceive/act-by-index browser wrapper. `AgentBrowser` / `agent_browser()` built on the snapshot layer. |
| [`snapshot`](wraith/snapshot.py) | Agent perception: `take_snapshot()` builds an indexed, browser-use-style DOM `Snapshot` of interactive `Element`s. |
| [`secrets`](wraith/secrets.py) | Opaque secret capabilities, provider registration, short-lived secret material, and policy errors. |
| [`recaptcha`](wraith/recaptcha.py) | v3 token harvesting from a warmed/borrowed session (`harvest_token`, `score`) + solver-service skeletons (`SolverService`, `CapSolver`, `TwoCaptcha`). |
| [`proxy`](wraith/proxy.py) | Dependency-free `ProxyPool` (round-robin / random) and `normalize_proxy()` for `clear_challenge` rotation. |
| [`providers`](wraith/providers.py) | First-class residential-proxy integrations. `DataImpulse` builds proxy URLs (`rotating`/`sticky`) and `ProxyPool`s (`pool`) for `launch(proxy=...)` / `clear_challenge(proxy_pool=...)`. |
| [`mcp`](wraith/mcp.py) | The `wraith-mcp` stdio server exposing the agent browser as MCP tools (runs on the `mcp` SDK 1.x FastMCP or 2.x MCPServer via a shim). |
| [`cli`](wraith/cli.py) | The `wraith` command. Lazily imports each component so `--help` works on a partial install. |

Runnable, heavily-commented examples live in [`examples/`](examples/):
`borrow_session.py`, `score_check.py`, `detect_waap.py`.

## How it gets past defenses (honest tiers)

Wraith uses the cheapest mechanism that works for each tier — there is no magic
universal bypass:

1. **Engine stealth (Camoufox / Firefox).** Firefox skips the Chrome-specific
   `isChrome()` detection cluster entirely, so the bulk of fingerprint tells
   never run. This alone clears solvable JS interstitials like Reblaze/Link11
   `ac_v2` — `clear_challenge()` just navigates and polls for the clearance
   cookie. No cookies, no proxy needed.
2. **Identity borrowing.** For reputation defenses (reCAPTCHA-v3) and
   already-authenticated areas of *your own* accounts, inject a warmed session
   from your real on-disk profile (`extract_cookies` → `inject_cookies`), or
   harvest a live bearer token (`SessionHarvester`). You borrow the reputation
   rather than trying to fake it.
3. **Residential exit rotation.** IP-reputation tiers (Reblaze 474/481
   rate-limit) can't be cleared by waiting or by cookies — they need a different
   exit IP. `ProxyPool` and the `DataImpulse` provider feed rotating/sticky
   residential exits into `launch(proxy=...)` and
   `clear_challenge(proxy_pool=...)`.
4. **Not solvable.** Hard blocks (HTTP 492, non-browser / `HeadlessChrome` UA)
   and reCAPTCHA-v3 with no warmed identity are *not* bypassable by Wraith —
   they raise actionable errors rather than pretending. reCAPTCHA-v3 has no
   client solver; `recaptcha.py` only harvests a token from a warmed session.

## Docs

- **[docs/DETECTION.md](docs/DETECTION.md)** — a WAAP / bot-system taxonomy with
  a quick-ID cheat sheet (header / cookie / status signals) and per-vendor deep
  dives (Reblaze/Link11 `ac_v2`, Akamai, reCAPTCHA-v3, DataDome, Imperva,
  Kasada, SiteMinder).
- **[docs/PLAYBOOK.md](docs/PLAYBOOK.md)** — the decision playbook: engine
  choice, the `playwright==1.55` pin, Chromium hardening, identity consistency,
  cookie extraction, live token harvesting, proxy rotation, and an end-to-end
  decision flow.
- **[docs/AGENTS.md](docs/AGENTS.md)** — the agent perception/action layer and
  the MCP server: snapshot format, acting by index, and wiring Wraith into an
  MCP client.

## Tests

```bash
uv run pytest -q
```

The suite runs fully offline (no browser binaries, no network): it asserts the
package imports cleanly, the public API symbols exist, and the detection /
identity / proxy logic behaves against synthetic inputs.

## Contributing

Contributions are welcome. Please:

1. Fork and branch from `main`.
2. `uv sync` and keep the suite green (`uv run pytest -q`).
3. Lint with `uv run ruff check .` before opening a PR.

Issues and PRs: <https://github.com/YogevKr/wraith>.

## License

MIT — see [LICENSE](LICENSE).
