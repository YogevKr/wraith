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
