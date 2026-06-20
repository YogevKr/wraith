# 🌀 Wraith

> A stealth, identity-borrowing, MCP-native agent browser.

[![CI](https://github.com/YogevKr/wraith/actions/workflows/ci.yml/badge.svg)](https://github.com/YogevKr/wraith/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

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
- **Live session harvesting** — latch the first request carrying both an
  `Authorization` header and a named auth cookie, for tokens never stored on
  disk.
- **WAAP/bot-defense fingerprinting** — `identify_waap()` recognises Reblaze/
  Link11, Akamai, reCAPTCHA, DataDome, Imperva/Incapsula, Kasada, SiteMinder.
- **Self-assessment** — read your own reCAPTCHA-v3 score and rebrowser
  automation tells.
- **Agent perception layer** — browser-use-style indexed DOM snapshots; act on
  elements by index (`click`, `type`, `scroll`, `read`).
- **MCP-native** — a stdio MCP server (`wraith-mcp`) exposing the agent browser
  as tools for any MCP client.
- **Residential proxy support** — a dependency-free `ProxyPool` plus a
  first-class DataImpulse provider for rotating/sticky exits.
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

The `wraith` console script has seven subcommands. The default engine is
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

# score   — read this identity's reCAPTCHA-v3 reputation (fresh -> ~0.1-0.3)
uv run wraith score
uv run wraith score --engine chromium --json

# detect  — fingerprint a URL's bot/WAAP defenses
uv run wraith detect https://example.com
uv run wraith detect https://example.com --json

# launch  — just open a stealth browser, held open (headed)
uv run wraith launch https://example.com
uv run wraith launch https://example.com --headless --no-wait

# mcp     — run the MCP server over stdio (see below)
uv run wraith mcp
```

> Chrome cookies are AES-encrypted via the OS keychain; Wraith deliberately does
> **not** decrypt them and raises `ChromeEncryptionError` with guidance. Use a
> **Firefox or Zen** profile (same engine family as Camoufox, plaintext-readable
> cookie store) — that's the recommended path — or harvest live.

### (c) MCP server

Wraith ships a stdio MCP server (`wraith-mcp`) that exposes the agent browser as
tools — `navigate`, `snapshot`, `click`, `type_text`, `scroll`, `read`,
`screenshot`, `detect_waap`, and `borrow`. Wire it into an MCP client such as
Claude Code:

```bash
claude mcp add wraith -- uv run --directory /path/to/wraith wraith-mcp
```

(Equivalently, the server starts via `uv run wraith mcp` or `uv run wraith-mcp`.)

## Architecture

Wraith is a set of focused, mostly-independent modules under `wraith/`:

| Module | Responsibility |
| --- | --- |
| [`engine`](wraith/engine.py) | Stealth launcher & engine selection. `launch()` / `browser()` return a `Session` (`.page`, `.context`, `.browser`); `clear_challenge()` is the cookie-free WAAP front door. Camoufox primary, patchright Chromium fallback; enforces the `playwright==1.55` pin. |
| [`identity`](wraith/identity.py) | **Signature feature.** Discover Firefox/Zen/Chrome profiles, `extract_cookies()`, normalize them, and `inject_cookies()` into a context. Firefox/Zen plaintext; Chrome raises `ChromeEncryptionError`. |
| [`harvest`](wraith/harvest.py) | Live session capture. `SessionHarvester` latches the first request carrying an `Authorization` header + auth cookie; `harvest_session()` is the high-level helper. |
| [`detect`](wraith/detect.py) | Diagnostics: `identify_waap()` (vendor fingerprinting), `recaptcha_v3_score()` (reputation read), `bot_detector()` (rebrowser automation tells), `fingerprint()`. |
| [`behavior`](wraith/behavior.py) | Human-like helpers: `human_move()` (curved/eased/jittered mouse), `human_type()` (per-key cadence), `dwell()`. |
| [`agent`](wraith/agent.py) | The perceive/act-by-index browser wrapper. `AgentBrowser` / `agent_browser()` built on the snapshot layer. |
| [`snapshot`](wraith/snapshot.py) | Agent perception: `take_snapshot()` builds an indexed, browser-use-style DOM `Snapshot` of interactive `Element`s. |
| [`recaptcha`](wraith/recaptcha.py) | v3 token harvesting from a warmed/borrowed session (`harvest_token`, `score`) + solver-service skeletons (`SolverService`, `CapSolver`, `TwoCaptcha`). |
| [`proxy`](wraith/proxy.py) | Dependency-free `ProxyPool` (round-robin / random) and `normalize_proxy()` for `clear_challenge` rotation. |
| [`providers`](wraith/providers.py) | First-class residential-proxy integrations. `DataImpulse` builds proxy URLs (`rotating`/`sticky`) and `ProxyPool`s (`pool`) for `launch(proxy=...)` / `clear_challenge(proxy_pool=...)`. |
| [`mcp`](wraith/mcp.py) | The `wraith-mcp` FastMCP stdio server exposing the agent browser as MCP tools. |
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
