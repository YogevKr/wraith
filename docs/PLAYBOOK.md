# PLAYBOOK.md — The Decision Playbook

> Given a target (identified via `DETECTION.md`), what do you actually do? This is
> the empirically-verified set of decisions, configs, and patterns that worked
> against EL AL's Reblaze/Link11 + reCAPTCHA v3 + Akamai + SiteMinder stack.

**The one-line philosophy:** *Don't beat reputation-based defenses head-on —
borrow a warmed, trusted identity, and choose an engine whose fingerprint never
contradicts itself.*

---

## 0. The layer model — three independent problems

Before picking a tactic, separate the three layers a WAAP stack can throw at
you. They are **independent**: each has its own signal and its own mitigation,
and a fix for one does nothing for the others. Diagnose which layer(s) you're
hitting first.

| Layer | What it is | Signal | Mitigation |
| --- | --- | --- | --- |
| **1. JS challenge** (general) | One-shot fingerprint + seed-keyed SHA-1 PoW, e.g. Reblaze `ac_v2` | HTTP **247** challenge | **Engine-solved, COOKIE-FREE.** A fresh Camoufox (Firefox) context solves it natively — Firefox skips the `isChrome()` detection cluster. No cookies, no warmed identity needed. |
| **2. Reputation gate** (per-site auth only) | reCAPTCHA v3 login score, warmed-account requirements | low v3 score (~0.1–0.3) on the gated step | **Borrow a warmed identity** (cookies from disk / harvested token). Not solvable; not an engine problem. |
| **3. IP rate-limit / reputation** | The exit IP has been hammered or is low-rep | HTTP **474 / 481** *instead of* the `247` challenge | **Rotate a residential proxy + back off.** Not an engine problem, not a cookie problem. |

The common mistake is conflating them: throwing proxies at a `247` (layer 1
solves cookie-free without them), or throwing a better engine at a `474` (layer
3 doesn't even serve the challenge). **ac_v2 itself is engine-solved
COOKIE-FREE; identity-borrowing is only for the reCAPTCHA-gated auth layer;
proxies are only for the IP tier.**

---

## 1. Engine choice — Firefox (Camoufox) beats Chromium

**Primary engine: Camoufox (Firefox-based stealth). Fallback: patchright
(patched Chromium).**

### Why Firefox wins
Both reCAPTCHA's client and Reblaze's `ac_v2` challenge branch on `isChrome()`.
In a Firefox engine that branch is **false**, so the entire Chrome-specific
detection cluster is **skipped**, including:
- `window.chrome === undefined` while the UA says Chrome (impossible to satisfy
  in headless Chromium without patching; a non-issue in Firefox).
- `HeadlessChrome` in the UA.
- `$cdc_*` / `__webdriver_*` / `__driver_*` ChromeDriver/automation leaks.

You don't *defeat* those checks — you make them **not run**. That is why Camoufox
is primary and Chromium is fallback, even though a well-patched Chromium can also
score high on general benchmarks.

### The hard version pin (this WILL bite you)
- **Camoufox 0.4.x CRASHES with `playwright>=1.60`.** It's a Firefox `pageError`
  serialization bug: `coreBundle.js` reads `pageError.location.url`, which is
  absent/changed in newer Playwright, throwing during page error handling.
- **=> Pin `playwright==1.55.x` for the Camoufox path** (or run Camoufox in its
  own venv isolated from a newer Playwright).
- **Detect and warn on mismatch** at startup: if the Camoufox path is selected
  and `playwright.__version__ >= 1.60`, refuse / warn loudly rather than crash
  cryptically mid-run.

### Chromium fallback hardening (when you must use patchright)
- Use the **patchright** backend — it suppresses the CDP `Runtime.enable` leak
  that vanilla Playwright-Chromium emits (caught by `rebrowser-bot-detector`'s
  `runtimeEnableLeak`).
- **`viewport=None`** — the Playwright default `1280x720` viewport is a **red
  flag** on `rebrowser-bot-detector`. `None` lets the real window size show
  through.
- **`ignore_default_args=['--enable-automation', '--enable-unsafe-swiftshader']`**
  — strip the automation banner flag and the SwiftShader flag that advertise a
  headless/software-GPU context.

---

## 2. Identity consistency (don't contradict yourself)

The kill signal for every fingerprint check is **internal contradiction**. Keep
every signal mutually consistent:

- **`geoip=True`** (Camoufox) — derive timezone + locale from the **exit IP**.
  If you proxy through a German IP, your timezone, locale, and `Accept-Language`
  must all read German. A US locale behind a German IP is a tell.
- **`locale` must match the region** you're presenting (and match
  `navigator.languages`, which Reblaze checks for consistency).
- **WebGL renderer:** AVOID software renderers in detection-sensitive contexts.
  The literal pair **`vendor == 'Brian Paul'` & `renderer == 'Mesa OffScreen'`
  is an instant headless tell** (it's check #4 in Reblaze's cluster).
  SwiftShader / ANGLE are softer, server-side-evaluated risks — prefer a real or
  realistically-spoofed GPU.
- **viewport / window:** real, non-default dimensions (see `viewport=None`).

---

## 2a. IP reputation / rate-limit (474 / 481)

This is **layer 3** (see §0): the exit IP itself is the problem, not your
browser. After an IP is hammered, Reblaze serves **HTTP 474 / 481 instead of
the `247` challenge** — the challenge isn't even served, so there is nothing for
the engine to solve. This is **distinct from a per-session block**: it's keyed
to the IP's reputation.

**Mitigation = rotating RESIDENTIAL proxy + backoff.** Not cookies, not engine
choice.

- Pass a proxy through the engine kwargs:
  `launch(..., proxy="http://user:pass@host:port")` (forwarded via `**kw`).
- **Use residential exits and rotate them**, with exponential backoff between
  attempts. Datacenter IPs start low-reputation and get rate-limited fast.
- **`geoip=True` makes the proxy safe to use:** it derives a consistent
  timezone + locale from the **proxy exit IP**, so rotating to a German exit
  automatically presents a German tz / locale / `Accept-Language`. Rotating the
  IP *without* `geoip=True` reintroduces the layer-1 contradiction the proxy was
  never meant to cause (US locale behind a German IP — see §2).

### How the engine surfaces it

`wraith.engine.clear_challenge()` distinguishes the layers by status code and
raises a typed exception:

- **`WaapRateLimitedError`** on **474 / 481** → the IP tier is exhausted;
  **rotate the proxy** (and back off). Retrying on the same IP, or "fixing" the
  engine, will not help.
- **`WaapHardBlockError`** on **492** → a gross engine tell (e.g.
  `HeadlessChrome` in the UA); fix the leak — rotating the proxy won't help
  here.

So the exception type tells you which layer you're on: `WaapRateLimitedError` ⇒
proxy problem (layer 3), `WaapHardBlockError` ⇒ engine problem (layer 1 tell).
A plain `247` needs neither — Camoufox solves it cookie-free.

### Proxy-pool rotation — let `clear_challenge` rotate for you

You don't have to drive rotation by hand. `proxy.ProxyPool` + the `proxy_pool`
parameter on `clear_challenge` automate the layer-3 retry:

```python
from wraith.proxy import ProxyPool
from wraith.engine import clear_challenge

pool = ProxyPool(
    [
        "http://user:pass@resi1.example:8000",
        "http://user:pass@resi2.example:8000",
        "resi3.example:8000",            # normalize_proxy() accepts host:port too
    ],
    strategy="round_robin",              # or "random"
)

# clear_challenge OWNS the session here (session=None), so it may rotate:
sess = clear_challenge("https://target.example/gated", proxy_pool=pool)
```

What it does, and the boundaries:

- On **`WaapRateLimitedError` (474/481)** or **`WaapHardBlockError` (492)**, if
  the pool has another proxy, `clear_challenge` **closes the owned session**,
  relaunches via `launch(engine=..., proxy=pool.next(), **launch_kw)`, and
  retries. It's **bounded by `len(proxy_pool)` attempts** and calls
  `pool.mark_bad(proxy)` on the proxy that just failed so it's skipped.
- It rotates **only when it OWNS the session** (`session is None`). **If you
  pass in a live `session`, rotation is skipped and the error re-raises** — you
  can't change a running session's exit IP, so swapping proxies mid-session is
  meaningless. Hand `clear_challenge` the *pool*, not a pre-launched session, if
  you want auto-rotation.
- Pair it with **`geoip=True`** (forwarded via `launch_kw`) so each new exit IP
  automatically presents a matching tz / locale / `Accept-Language` — otherwise
  rotation reintroduces the layer-1 contradiction (see §2).
- `492` is technically an engine tell, but it's included in the rotation set
  because a low-rep IP can *also* surface as a hard block; rotating costs one
  attempt and `mark_bad` retires the bad exit either way. If every proxy is
  exhausted the original typed error propagates.

This keeps the **layer separation** intact: the pool only ever helps the IP
tier; it does nothing for a clean `247` (no rotation needed) or a reputation
gate (borrow identity instead).

### DataImpulse residential proxies

`wraith.providers.DataImpulse` is a first-class **pay-per-GB residential**
provider that turns a single account into the proxy URL strings (and
`ProxyPool`s) the layer-3 machinery above consumes. You authenticate to one
gateway and steer the exit IP entirely through the *username*.

**Credentials** resolve most-specific-first and **never raise at construction**
— a `DataImpulseAuthError` is raised lazily only when you actually request a URL:

1. explicit `DataImpulse(username=..., password=...)`;
2. env `DATAIMPULSE_USERNAME` / `DATAIMPULSE_PASSWORD`;
3. `~/.secrets` (`KEY=value` lines, tolerates `export ` + quotes).

**Rotating vs. sticky.** A *base* username (no `sessid`) rotates the exit IP on
**every request**; add a `sessid` and the IP is **sticky** (~30 min, same IP):

```python
from wraith.providers import DataImpulse

di = DataImpulse(country="il")                 # creds from env / ~/.secrets
di.rotating()                                  # new IL IP per request
# -> http://<user>__cr.il:<pw>@gw.dataimpulse.com:823
di.sticky("profile01")                         # one pinned IL IP for ~30 min
# -> http://<user>__cr.il;sessid.profile01:<pw>@gw.dataimpulse.com:823
di.rotating(city="newyork", country="us")      # city pin
DataImpulse(protocol="socks5").rotating()      # SOCKS5 -> port 824
```

The enrichment format is the base username, then `__`, then `;`-joined
`key.value` params (`cr` country, `city`, `sessid`). HTTP/HTTPS use port **823**,
SOCKS5 uses **824**.

**`.pool()` with `clear_challenge`.** `di.pool(n)` mints `n` *distinct* sticky
sessions (`wraith-0`..`wraith-(n-1)`) → `n` different exit IPs the `ProxyPool`
can rotate across when retrying a 474/481/492:

```python
from wraith.engine import clear_challenge
from wraith.providers import DataImpulse

sess = clear_challenge(
    "https://target.example/gated",
    proxy_pool=DataImpulse(country="il").pool(5),  # 5 distinct sticky IL exits
    geoip=True,
)
```

(`pool(n, sticky=False)` instead returns the rotating endpoint, which de-dupes
to a single pool entry since every request through it already rotates.)

**geoip note.** Pair any DataImpulse proxy with `geoip=True` (the default): the
engine derives a coherent timezone / locale / `Accept-Language` from the proxy
*exit IP* (§2). A **sticky** session keeps that identity stable for its whole
lifetime — prefer it over rotating when you need a consistent identity across
several navigations. From the CLI, `--proxy dataimpulse` (or `--dataimpulse`)
with `--proxy-country il` builds a rotating IL exit for `launch`/`borrow`/
`harvest`/`agent`.

---

## 2b. Akamai `_abck` validity — `~0~` solved, `~-1~` is NOT

A clearance cookie's *presence* is enough for most vendors, but **Akamai
`_abck` is special** and getting this wrong silently reports a non-existent
pass:

- A **fresh / unsolved** `_abck` value contains **`~-1~`** — the cookie is set
  but the sensor data hasn't been accepted yet. **This is not cleared.**
- A **solved** `_abck` value contains **`~0~`** (and no `~-1~`). Only now is the
  session trusted.

`clear_challenge` treats a clearance cookie as success **only if**
`detect.cookie_is_valid(name, value)` is True, and `cookie_is_valid` enforces
the `_abck` `~0~`/`~-1~` rule. So a page that hands you a `~-1~` `_abck` keeps
polling (or times out) rather than declaring victory — which is exactly why
Akamai is **Tier 2**: it needs the behavioral nudge to *flip* `_abck` to `~0~`,
not merely to receive it.

### The behavioral nudge (built into `clear_challenge`)

Before the poll loop, `clear_challenge` makes a **best-effort** behavioral nudge:
`behavior.human_move(page)` + a short dwell, wrapped so any failure is
non-fatal. This nudges the score-based defenses (Akamai, DataDome, PerimeterX)
toward minting/flipping their trust cookie. It is *not* theatrical event
injection — see §6 — just enough entropy to look alive.

---

## 3. reCAPTCHA v3 — borrow, don't beat (the signature pattern)

reCAPTCHA v3 is a **reputation score**, not a puzzle (see `DETECTION.md §3`). A
fresh automated profile scores ~0.1–0.3 no matter how good your engine or mouse
movement is; a real warmed browser scores ~0.9. **You cannot fake it.**

**=> The winning pattern: inject a warmed identity and skip the
reCAPTCHA-gated step entirely.**

1. Extract the user's **live session cookies** from their *real* browser profile
   on disk (see §4).
2. Inject them into the stealth context via `context.add_cookies(...)`.
3. Navigate as the already-authenticated user — the reCAPTCHA-gated login is
   never exercised, so its score never matters.

This converts an unwinnable reputation problem into a one-time identity-copy
problem. It is the core of Wraith.

> Necessary-but-not-sufficient: a clean engine still matters (a 492-level tell or
> a `webdriver` leak will sink even a borrowed identity). Identity borrowing
> removes the reputation wall; engine stealth keeps you from tripping the static
> gates.

---

## 4. Identity borrowing — extracting cookies from real profiles

### Firefox / Zen
- Cookies live at `…/Profiles/<profile>/cookies.sqlite`, table `moz_cookies`.
  Useful columns: `host`, `name`, `value`, `path`, `isSecure`, `isHttpOnly`,
  `sameSite`.
- **The DB is locked while the browser is running** — copy `cookies.sqlite`
  **and** its `-wal` sidecar first, then read the copy (otherwise you miss
  recent writes still in the WAL or hit a lock).
- **Firefox `sameSite` encoding:** `0 = None`, `1 = Lax`, `2 = Strict`.

### Chrome / Chromium
- Cookies at `…/Default/Cookies` (SQLite). On macOS the `value` column is
  **AES-encrypted via the OS keychain** — note this; decryption is an advanced
  extra (keychain access + AES-GCM with the Chrome Safe Storage key). Plan for it
  but treat it as opt-in.

### Injecting via Playwright
- Use `context.add_cookies([...])` and **map `sameSite` correctly** to
  Playwright's `'None' | 'Lax' | 'Strict'`.
- **`sameSite='None'` requires `secure=True`** — Playwright (and browsers) reject
  a `None` cookie that isn't secure. Translate Firefox `0 → 'None'` and set
  `secure=True` for it.
- After injecting, navigate straight to the authenticated area.

---

## 5. Harvesting tokens that aren't cookies

Many auth bearer tokens are **not cookies** — they're minted per session and sent
as an `Authorization` header. Cookie copying alone won't carry them.

**Harvest them live from network traffic:**
- Listen with `context.on('request', …)`.
- Capture the **first** request to the target API that carries both an
  `Authorization` header **and** the auth cookie.
- Persist a reusable session file: `{ Authorization, Cookie, User-Agent }`.
- Replay that triplet on subsequent (even non-browser, e.g. `httpx`) calls to act
  as the authenticated user. Keep the `User-Agent` consistent with what minted
  the token.

So Wraith supports **two** identity sources: borrow cookies from disk (§4) **and**
harvest live bearer tokens from the network (§5).

---

## 6. Behavior tips

Reblaze's `ac_v2` has **no behavioral tracking** (it's a one-shot fingerprint +
PoW), so you don't need human-like behavior *for Reblaze*. But Akamai / DataDome /
reCAPTCHA-the-score do weigh behavior, so for those:

- **Synthetic `page.mouse.move` entropy HELPS** scores — a little realistic mouse
  motion is positive signal.
- **Instant `page.fill` of credentials is robotic** — a field that goes from empty
  to fully-populated in one tick is a tell. Provide human-like **type + move**
  helpers (per-keystroke delays, cursor movement between fields).
- **Do NOT over-inject obviously-synthetic events.** Mechanical, perfectly-timed
  "human" events are worse than none. Aim for plausible, not theatrical.

---

## 6a. Tier strategy — match effort to the tier (`DETECTION.md` coverage matrix)

Each vendor in the coverage matrix is graded by **how hard it is to get
through**, and each tier maps to a *specific* tactic. Don't over-invest on a
Tier 1, and — critically — **don't expect to brute Tier 3.**

### Tier 1 — engine + resi-IP (e.g. Reblaze `ac_v2`, Cloudflare managed challenge)
- A clean **Camoufox** (primary) or **patchright** (fallback) context clears it
  **natively, often cookie-free**, plus a decent residential exit IP.
- No behavior loop, no warmed identity, no solver. For Reblaze the engine's
  `isChrome()` short-circuit *is* the whole bypass (§1).
- The only common failure here is the **IP tier** (474/481) — that's layer 3,
  fixed by `proxy_pool` rotation (§2a), not by more engine work.

### Tier 2 — behavioral nudge + solved-state cookie (Akamai, DataDome, PerimeterX, Incapsula, BIG-IP)
- Clean engine **plus** a modest **behavioral nudge**: `clear_challenge` runs
  `behavior.human_move(page)` + a short dwell before polling (§2b, §6), which
  pushes the score-based defenses toward minting/flipping their trust cookie.
- **Cookie validity, not presence:** success counts only when
  `detect.cookie_is_valid(name, value)` is True. The headline case is Akamai
  **`_abck`**, valid only at **`~0~`** (a fresh **`~-1~`** is unsolved — see
  §2b). This is why Tier 2 needs the nudge: to *flip* the cookie, not just
  receive it.
- If clean-engine + nudge still fails (DataDome/Cloudflare drops to an
  interactive CAPTCHA, PX gets stubborn), it has effectively become **Tier 3** —
  fall back to identity borrowing.

### Tier 3 — needs an outside solver or a borrowed identity (reCAPTCHA v3, hCaptcha, Kasada, F5 Shape, the CAPTCHA path of DataDome/Cloudflare/AWS WAF)
- **Wraith does NOT solve these head-on. There is no built-in solver.** Be
  honest about this in any automation that hits a Tier 3 — don't promise a clear.
- Realistic routes, in order of preference:
  1. **Borrow a warmed identity** — inject real session cookies (§4) or harvest
     the `{Authorization, Cookie, User-Agent}` triplet (§5) and **skip the gated
     step entirely.** This is the signature Wraith move and the *only* answer for
     a reputation score like reCAPTCHA v3 (it has no solver, you cannot fake it).
  2. **An external CAPTCHA-solving service** for interactive widgets
     (hCaptcha / reCAPTCHA-v2 / Turnstile challenge) — an *outside* dependency,
     not something Wraith does internally.
- **Kasada and F5 Shape are VM-grade integrity systems** — assume no casual
  bypass at all; identity borrowing / token harvesting is the realistic path.
- A clean engine is still **necessary-but-not-sufficient** here (a `webdriver`
  or 492-level tell sinks even a borrowed identity), but it is never *sufficient*
  on its own for Tier 3.

> **Do not overpromise Tier 3.** A truthful status for a Tier-3 target is
> "blocked — needs a borrowed identity or an external solver," never "will
> clear automatically."

---

## 7. Decision flow (putting it together)

1. **Identify the system** (`DETECTION.md` cheat sheet) from headers/cookies/status,
   and **classify the layer** (§0): JS challenge (247) / reputation gate /
   IP rate-limit (474/481).
2. **Pick the engine:** Camoufox primary (with `playwright==1.55` pinned);
   patchright fallback (`viewport=None`, suppress `Runtime.enable`, strip
   automation args). For a plain `247` this alone gets you through — Camoufox
   solves `ac_v2` cookie-free.
3. **Set identity consistency:** `geoip=True`, matching locale, no
   `Brian Paul`/`Mesa OffScreen` WebGL.
4. **If you get 474/481 (`WaapRateLimitedError`) → rotate a residential proxy +
   back off** (§2a). `geoip=True` keeps tz/locale consistent with the new exit
   IP. Don't touch the engine for this.
5. **If you get 492 (`WaapHardBlockError`) → fix the engine tell** (e.g.
   `HeadlessChrome` UA); a proxy won't help.
6. **If a reputation gate (reCAPTCHA v3 / warmed-account requirement) blocks the
   path → borrow identity** (cookies from disk, §4) and skip the gated step.
7. **If auth is a bearer token, not a cookie → harvest** the
   `{Authorization, Cookie, User-Agent}` triplet live (§5).
8. **If a behavioral system (Akamai/DataDome) → add modest mouse/typing entropy**,
   never theatrical events (§6).
9. **Self-check** against `rebrowser-bot-detector` and the reCAPTCHA-v3 score
   tester (parse only the fresh `Result:` line) before trusting the session.

---

## 8. Benchmark numbers (techinz/browsers-benchmark, general protections)

| Engine | Pass rate |
| --- | --- |
| **patchright** | ~100% |
| **CloakBrowser** | ~90% |
| **Camoufox (headless)** | ~90% |
| plain Playwright | ~40% |

Notes:
- **Camoufox headless > Camoufox headed** — its anti-fingerprinting is most
  complete in headless mode; don't assume headed is "more human".
- These are *general* benchmark scores. Against **Reblaze/Link11 specifically
  there is no public bypass** and these numbers don't apply — the engine-choice
  (`isChrome()` short-circuit) + identity-borrowing approach is what actually got
  through. Plain Playwright's ~40% reflects exactly the contradictions (default
  viewport, `Runtime.enable` leak, `webdriver`, Chrome-vs-headless mismatch) that
  the hardening in §1–§2 removes.
