# 🌀 Wraith

> The identity-borrowing stealth browser for autonomous agents.

Wraith is a stealth browser toolkit for autonomous AI agents. Instead of
fighting reputation-based defenses head-on, it pairs a Firefox-engine stealth
browser with *identity borrowing* — reusing a warmed, already-authenticated
session from a real browser profile (or harvesting live auth tokens) so the
agent acts as a trusted, established user rather than a fresh bot.

## Why "Wraith"?

A wraith passes through walls unseen and wears another's likeness. That is
exactly the strategy: rather than brute-forcing the gate, Wraith slips through
by *borrowing the identity* of someone already inside — a warmed, trusted
browser profile — and moves as that user. It is a ghost wearing a real
reputation.

## Why identity borrowing (not "solving")

Modern defenses like **reCAPTCHA-v3** and **Reblaze/Link11 `ac_v2`** are
*reputation* systems, not puzzles. reCAPTCHA-v3 has **no solver**: it returns a
`0.0..1.0` score derived from your Google-account cookies, aged browsing
history, and IP. A fresh automated profile scores ~0.1–0.3 ("bot") no matter
how good the stealth engine is, because it has no history to vouch for it. A
real human's warmed-up browser scores ~0.9. You cannot fake reputation.

So Wraith doesn't try. It **borrows a warmed identity**: it reads the user's
live session cookies straight out of their real browser profile on disk (or
harvests a live bearer token from network traffic) and injects them into the
stealth context. The agent then navigates as the already-authenticated, already
trusted user — skipping the reCAPTCHA-gated login entirely. The reputation
comes along for free.

### Engine: Camoufox (Firefox) is **primary**

Wraith's default engine is **[Camoufox](https://camoufox.com/)**, a hardened
Firefox build. Firefox is the *right* engine against fingerprint/reputation
defenses: the challenge JS takes its Chrome-specific detection branch only when
`isChrome()` is true, so under Firefox the entire Chrome detection cluster
(`window.chrome === undefined` while the UA claims Chrome, `HeadlessChrome` UA,
`$cdc_`/`__*driver_*` leaks) is simply never run. Camoufox **headless** even
benchmarks *higher* than headed.

**patchright-Chromium** is the fallback (patched Playwright Chromium that
suppresses the `Runtime.enable` CDP leak, forces `viewport=None`, and strips
`--enable-automation`). It is weaker against reCAPTCHA-v3 / Reblaze.

> ⚠️ Camoufox 0.4.x crashes on `playwright >= 1.60` (a Firefox `pageError`
> serialization bug). Wraith pins **`playwright == 1.55.x`** and detects a
> mismatch up front with an actionable error. patchright is independently
> versioned and unaffected.

## Install

Wraith uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync                 # core deps (camoufox, playwright==1.55, patchright, httpx)
uv sync --extra dev     # + pytest for the test suite
```

Browser binaries are runtime installs (not pip deps):

```bash
uv run camoufox fetch              # fetch the Camoufox Firefox build (primary)
uv run patchright install chromium # fetch patched Chromium (fallback)
```

Verify the install:

```bash
uv run python -c "import wraith; print(wraith.__version__, wraith.__all__)"
uv run wraith --help
```

## Quickstart (CLI)

The `wraith` console script (a.k.a. `python -m wraith.cli`) has five
subcommands. The default engine is `camoufox`; pass `--engine chromium` for the
patchright fallback or `--engine auto` to let Wraith choose.

### `wraith detect` — fingerprint the defenses

Find out *what* guards a URL before you spend a session on it. Header / cookie /
status / body driven; recognises Reblaze/Link11, Akamai, reCAPTCHA, DataDome,
Incapsula/Imperva and SiteMinder.

```bash
uv run wraith detect https://www.elal.com/
uv run wraith detect https://example.com --json
```

### `wraith score` — read your reCAPTCHA-v3 reputation

Measures the current identity's reCAPTCHA-v3 score (0.0 = bot … 1.0 = warmed)
via cleantalk's tester. A low score is the *expected* baseline for a fresh
profile — it's the signal to switch to identity borrowing, not a stealth bug.

```bash
uv run wraith score                  # fresh profile -> expect ~0.1-0.3
uv run wraith score --engine chromium --json
```

### `wraith borrow` — the signature flow

Extract cookies from a real on-disk browser profile, inject them, and open the
target as the already-authenticated user. Auto-detects Zen → Firefox → Chrome,
or pass `--profile` with a browser name or an explicit profile-directory path.

```bash
# auto-detect a profile, borrow elal.com cookies, open the site as that user
uv run wraith borrow https://www.elal.com/ --host elal.com

# point at a specific profile
uv run wraith borrow https://www.elal.com/ \
    --profile "~/Library/Application Support/zen/Profiles/xxxx.default"
```

> Chrome cookies are AES-encrypted via the OS keychain; Wraith deliberately does
> **not** decrypt them and raises `ChromeEncryptionError` with guidance. Use a
> **Firefox or Zen** profile (same engine family as Camoufox, plaintext-readable
> cookie store) — that's the recommended path — or harvest live (below).

### `wraith harvest` — capture a live auth session

Some bearer tokens are minted per-session and sent as an `Authorization`
header, never stored as a cookie. `harvest` opens the target, watches network
traffic, and captures the first request carrying both the auth header and the
named auth cookie, saving `{Authorization, Cookie, User-Agent}` to a JSON file.

```bash
uv run wraith harvest https://www.elal.com/ \
    --target booking.elal.com \
    --cookie rbzid \
    -o elal.session.json

# optionally seed cookies from a real profile before navigating:
uv run wraith harvest https://www.elal.com/ --borrow zen --target booking.elal.com
```

Known auth-cookie names worth trying: `rbzid`, `waap_id`, `SMSESSION`,
`datadome`, `_abck`.

### `wraith launch` — just open a stealth browser

```bash
uv run wraith launch https://bot-detector.rebrowser.net/   # headed, holds open
uv run wraith launch https://example.com --headless --no-wait
```

## Quickstart (library)

```python
import wraith

# 1. Launch a stealth browser (context-managed -> auto-closes).
with wraith.browser(engine="camoufox", geoip=True) as s:
    # 2. Borrow a warmed identity from a real Zen/Firefox profile.
    profile = wraith.find_zen_profiles()[0]
    cookies = wraith.extract_cookies(profile, domain_filter="elal.com")
    wraith.inject_cookies(s.context, cookies)

    # 3. Navigate as the already-authenticated user.
    s.page.goto("https://www.elal.com/")

    # 4. (Optional) harvest the live bearer token the app mints.
    h = wraith.SessionHarvester(target_url="booking.elal.com", auth_cookie="rbzid")
    h.attach(s.context)
    s.page.goto("https://www.elal.com/booking")
    h.wait(timeout=60)
    h.save_session("elal.session.json")
```

`import wraith` is resilient: if an optional browser dependency is missing, the
affected symbols are simply omitted and `wraith.missing_imports` records why —
a partial install never breaks the import.

## Architecture

Wraith is a small set of focused, mostly-independent modules under `wraith/`:

| Module | Responsibility |
| --- | --- |
| [`wraith/engine.py`](wraith/engine.py) | Stealth launcher & engine selection. `launch()` / `browser()` return a `Session` (`.page`, `.context`, `.browser`). Camoufox primary, patchright Chromium fallback; enforces the `playwright==1.55` pin. |
| [`wraith/identity.py`](wraith/identity.py) | **Signature feature.** Discover Firefox/Zen/Chrome profiles, extract cookies (`extract_cookies`), normalize them, and inject into a context (`inject_cookies`). Firefox/Zen are plaintext; Chrome raises `ChromeEncryptionError`. |
| [`wraith/harvest.py`](wraith/harvest.py) | Live session capture. `SessionHarvester` latches the first request carrying an `Authorization` header + auth cookie; `harvest_session()` is the high-level CLI helper. |
| [`wraith/detect.py`](wraith/detect.py) | Diagnostics: `identify_waap()` (vendor fingerprinting), `recaptcha_v3_score()` (reputation read), `bot_detector()` (rebrowser automation tells). |
| [`wraith/behavior.py`](wraith/behavior.py) | Human-like helpers: `human_move()` (curved/eased/jittered mouse), `human_type()` (per-key cadence), `dwell()`. The supporting act, not the headliner. |
| [`wraith/cli.py`](wraith/cli.py) | The `wraith` command. Lazily imports each component so `--help` works on a partial install. |

The `identity`, `behavior`, `harvest` and `detect` modules are duck-typed
against the Playwright sync API and import without any browser installed.

Runnable, heavily-commented examples live in [`examples/`](examples/):
`borrow_session.py`, `score_check.py`, `detect_waap.py`.

## Docs

Two deep-dive references encode the hard-won, empirically-verified findings
from driving EL AL's Reblaze/Link11 + reCAPTCHA-v3 + Akamai + SiteMinder stack:

- **[docs/DETECTION.md](docs/DETECTION.md)** — a WAAP / bot-system taxonomy with
  a quick-ID cheat sheet (header / cookie / status signals) and per-vendor deep
  dives (Reblaze/Link11 `ac_v2`, Akamai, reCAPTCHA-v3, DataDome, Imperva,
  Kasada, SiteMinder).
- **[docs/PLAYBOOK.md](docs/PLAYBOOK.md)** — the decision playbook: engine
  choice, the `playwright==1.55` pin, Chromium hardening, identity consistency,
  cookie extraction, live token harvesting, behavior tips, and an end-to-end
  decision flow.

## Tests

```bash
uv run python -m pytest tests/ -q
```

The smoke suite runs fully offline (no browser binaries, no network): it
asserts the package imports cleanly, the public API symbols exist, and
`identify_waap` correctly fingerprints a synthetic header/cookie set.

## License

MIT — see [LICENSE](LICENSE).
