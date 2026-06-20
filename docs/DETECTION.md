# DETECTION.md — Bot/WAAP Taxonomy & Identification

> How to recognize each bot-management / WAAP system, what it actually checks,
> and what that means for getting through it. This is the distilled,
> empirically-verified record (validated against EL AL's live stack:
> Reblaze/Link11 + Google reCAPTCHA v3 + Akamai Bot Manager + CA SiteMinder).

The single most useful skill is **identifying which system you are facing** from
response headers, cookies, status codes, and injected JS globals — because the
*right* approach differs wildly per system (a one-shot fingerprint check is
beaten differently than a reputation score). Identify first, then choose a
strategy from `PLAYBOOK.md`.

---

## Quick identification cheat sheet

| System | Tell-tale signal | Where to look |
| --- | --- | --- |
| **Reblaze / Link11** | `server: rhino-core-shield`; clearance cookies `waap_id`, `rbzid`; statuses **247/248/492** and **474/481** (IP rate-limit); JS sets `window.rbzns = {seed, bereshit:'1'}` + `winsocks()` | Response headers, cookies, challenge body |
| **Akamai Bot Manager** | cookies `_abck`, `bm_sz`, `AKA_A2`; header `x-akamai-transformed` | Response headers, cookies |
| **Google reCAPTCHA v3** | `grecaptcha` JS globals; `https://www.google.com/recaptcha/api.js?render=<sitekey>`; tokens posted as `g-recaptcha-response` | Page DOM/JS, network |
| **DataDome** | cookie `datadome`; header `x-datadome` / `x-dd-b` | Cookies, headers |
| **Imperva / Incapsula** | cookies `visid_incap_*`, `incap_ses_*`, `reese84`; header `x-iinfo`; `X-CDN: Incapsula` | Cookies, headers |
| **Kasada** | header `x-kpsdk-ct` / `x-kpsdk-cd`; script `/ips.js`; POST to `/tl` | Headers, network |
| **CA/Broadcom SiteMinder** | cookie `SMSESSION`; redirects through `/siteminderagent/`; `SMCHALLENGE` | Cookies, redirect chain |

---

## 1. Reblaze / Link11 (`ac_v2`) — the headline target

Reblaze (acquired by Link11) fronts the EL AL stack. There is **no public
bypass** — even commercial multi-WAF evasion SDKs explicitly skip it. What makes
it beatable in practice is what it *doesn't* do.

### How to identify it
- **Server header:** `server: rhino-core-shield` (rhino = Reblaze's engine).
- **Cookies (clearance):** `waap_id` (visitor id) and `rbzid` (the
  validated-token cookie) — these are the clearance cookies the solved challenge
  mints.
- **Status-code ladder (Reblaze-specific, non-standard):**
  - **247** — challenge required (serve the JS challenge page; sets
    `window.rbzns{seed, bereshit:'1'}` + `winsocks()`).
  - **248** — token / verification exchange (client submitting the solved
    hashcash back; clears to **200** with the `waap_id` / `rbzid` cookies set).
  - **492** — hard block. Triggered by gross tells, e.g. a `User-Agent`
    containing `HeadlessChrome`. **No challenge is served** — you were caught at
    the cheapest possible layer; fix the obvious leak before anything else.
  - **474 / 481 — IP rate-limit tier (NEW).** Served *instead of* a `247`
    after an IP has been hammered: the challenge **isn't even served**, so this
    is **not** something a better engine or a fresh context can solve. It is
    **distinct from a per-session block** — it's keyed to the **exit IP's
    reputation**, not your browser. Mitigation is **proxy rotation + backoff**,
    not cookies and not engine choice (see `PLAYBOOK.md` — IP reputation /
    rate-limit).

### The challenge mechanism (`ac_v2`)
- The challenge page sets `window.rbzns = { seed: <…>, bereshit: '1' }` and calls
  `winsocks()`. (`bereshit` — Hebrew for "Genesis/in-the-beginning" — flags that
  the challenge runtime has initialized.)
- **It is a one-shot fingerprint + a seed-keyed SHA-1 hashcash (proof-of-work).**
  The client gathers a fingerprint, runs the 10 bot-checks below, and computes a
  SHA-1-based proof-of-work keyed by `seed`. On success the server issues the
  `rbzid` token.
- **CRITICAL: there is NO behavioral / mouse-movement / timing tracking in
  `ac_v2`.** It is a single static gate, not a continuous trust score. This is
  the whole reason a stealth browser can pass it: get the fingerprint clean once,
  solve the PoW, and you are through. No need to simulate human behavior over
  time for Reblaze itself.

### The 10 bot-checks (the fingerprint cluster you must survive)
These are the checks the `ac_v2` challenge runs client-side. Most are
**Chrome-specific**, which is exactly why a Firefox engine sidesteps the whole
cluster (see `PLAYBOOK.md` — the `isChrome()` short-circuit):

1. `window.chrome === undefined` **while** the User-Agent claims Chrome — the
   classic headless-Chromium contradiction.
2. User-Agent contains `HeadlessChrome` (also triggers the 492 hard block).
3. WebDriver / automation artifacts: `$cdc_*` (ChromeDriver-injected globals),
   `__webdriver_*`, `__driver_*`, `__$webdriverAsyncExecutor`, etc.
4. WebGL renderer/vendor pair equal to `Brian Paul` / `Mesa OffScreen` — the
   literal software-renderer signature of a headless/virtual GPU. **Instant
   tell.** (SwiftShader / ANGLE are softer, server-side-evaluated risks.)
5. "Fake image" probe — an `Image` whose `naturalWidth`/`naturalHeight` come back
   `0x0` where a real render would be non-zero.
6. `navigator.languages` consistency — must be present, non-empty, and consistent
   with the declared locale / `Accept-Language`.
7–10. Remaining fingerprint-consistency checks in the same family (plugins,
   permissions, `navigator.webdriver`, UA-vs-platform agreement). The unifying
   theme: **the browser must not contradict itself.** Any internal
   inconsistency (UA says X, runtime says Y) is the kill signal.

### What this means strategically
Because the cluster is overwhelmingly Chrome-targeted and there is no behavioral
layer, the winning move is a **Firefox-engine stealth browser (Camoufox)**: the
challenge's `isChrome()` branch evaluates false, so the entire Chrome detection
cluster (#1–#3 above) is never executed. See `PLAYBOOK.md §1`.

---

## 2. Akamai Bot Manager

Akamai is a heavyweight, behavioral + fingerprint hybrid. Much harder than
Reblaze's one-shot gate because it scores continuously.

### How to identify it
- **Cookies:** `_abck` (the primary bot cookie — its value encodes whether you've
  passed), `bm_sz` (Bot Manager session), `AKA_A2`.
- **Header:** `x-akamai-transformed` on responses fronted by Akamai.
- Sensor data is POSTed to an endpoint (often a path the page's Akamai script
  registers) carrying an obfuscated, telemetry-rich payload.

### What it checks
- A large device/browser fingerprint **plus** behavioral sensor data (input
  cadence, event ordering, timing). Unlike Reblaze, Akamai *does* track behavior,
  so synthetic-but-plausible interaction helps and instant robotic actions hurt.
- The `_abck` cookie transitions from an "unvalidated" to a "validated" state
  only after acceptable sensor data is submitted.

### Strategy note
No one-shot trick. Treat as: clean fingerprint (Camoufox/patchright) + realistic
interaction + correct sensor submission. If you only need a session, prefer
**identity borrowing / token harvesting** over solving Akamai cold.

---

## 3. Google reCAPTCHA v3 — *the* conceptual key

reCAPTCHA **v3 is not a challenge you solve — it is a reputation score.** This
distinction drives the entire playbook.

### How to identify it
- `grecaptcha` global on the page; the script
  `https://www.google.com/recaptcha/api.js?render=<sitekey>`.
- No visible checkbox/puzzle (that's v2). v3 runs invisibly and emits a token.
- The site posts a `g-recaptcha-response` token to its backend, which calls
  Google's `siteverify` and reads back a **score in `0.0 .. 1.0`** plus an
  `action`.

### What it actually measures
- A **reputation score** derived from: Google account cookies, aged browsing
  history, IP reputation, and prior behavior — *not* from anything you do on the
  page in the moment.
- **A fresh automated profile scores ~0.1–0.3 (bot) regardless of stealth engine
  or how human your mouse looks. A real, warmed, logged-in browser scores ~0.9.**
- **There is NO solver. You cannot fake the score.** Engine quality is necessary
  (a 492-level tell will tank it) but nowhere near sufficient.

### Strategy note (full detail in `PLAYBOOK.md §3`)
**Don't beat reCAPTCHA v3 — borrow a warmed identity.** Extract the user's live
session cookies from their real browser and inject them, skipping the
reCAPTCHA-gated step entirely. This is Wraith's signature pattern.

#### Diagnosing your own score (important gotcha)
To self-assess, use a v3 score tester (e.g. `cleantalk.org/recaptcha-v3-score-test`).
**Parse ONLY the live `Result: X | Time: … | Hostname: …` line and verify the
timestamp is fresh.** Do NOT regex the page generically:
- The page's FAQ literally contains the string `0.9` → a generic regex reads a
  false high score.
- The page caches a stale "Last Score" → you read an old result.
Both of these produced false positives during testing. Bind to the fresh
timestamped result line or trust nothing.

---

## 4. DataDome

### How to identify it
- **Cookie:** `datadome`.
- **Headers:** `x-datadome`, sometimes `x-dd-b`; challenge pages reference
  `js.datadome.co` / `geo.captcha-delivery.com`.

### What it checks
- Fingerprint + behavioral scoring, similar in spirit to Akamai. Serves a
  device-check or CAPTCHA interstitial when suspicious. The `datadome` cookie
  carries the trust verdict across requests.

---

## 5. Imperva / Incapsula

### How to identify it
- **Cookies:** `visid_incap_<siteid>`, `incap_ses_<…>`, and the newer `reese84`
  (Incapsula's advanced JS-challenge token).
- **Headers:** `x-iinfo`, `X-CDN: Incapsula`.
- Classic interstitial: "Request unsuccessful. Incapsula incident ID …".

### What it checks
- JS challenge that mints `reese84` after running a fingerprint/PoW-style script;
  `visid_incap` then identifies the validated visitor. Fingerprint-consistency is
  the main lever.

---

## 6. Kasada

### How to identify it
- **Headers:** `x-kpsdk-ct`, `x-kpsdk-cd`, `x-kpsdk-v`.
- A script typically served from `/ips.js`; the client POSTs to a `/tl`
  ("telemetry") endpoint to obtain clearance tokens.

### What it checks
- Heavily obfuscated, VM-based client integrity + PoW + fingerprint. Among the
  hardest. No casual bypass; identity borrowing / token harvesting is the
  realistic route if you must get a session.

---

## 7. CA / Broadcom SiteMinder

This is an **access-management / SSO** product, not a bot-management WAAP — but
it shows up in the same enterprise stacks (it does in EL AL's) and you must
recognize it because it gates authenticated routes.

### How to identify it
- **Cookie:** `SMSESSION` (the SiteMinder session token; `SMSESSION=LOGGEDOFF`
  means no/expired session).
- **Redirects through** `/siteminderagent/` paths; `SMCHALLENGE`,
  `SMIDENTITY` artifacts.

### What it checks
- It does **not** fingerprint the browser for bot-ness; it enforces
  authentication/authorization. A valid `SMSESSION` cookie = an authenticated
  user. This is the cookie most worth **borrowing/harvesting** to act as a
  logged-in user (see `PLAYBOOK.md §3–4`).

---

## Self-assessment / diagnostics toolkit

So the toolkit can grade its own stealth before hitting a real target:

- **`rebrowser-bot-detector` (`bot-detector.rebrowser.net`)** — checks:
  - `runtimeEnableLeak` — CDP `Runtime.enable` leak (patchright suppresses this).
  - `navigatorWebdriver` — `navigator.webdriver === true`.
  - `viewport` — flags the **Playwright default `1280x720`** as a bot signal →
    use `viewport=None`.
  - `pwInitScripts` — Playwright init-script residue.
  - `dummyFn` — exposed function leak.
  - `sourceUrlLeak` — injected-script source-URL leak.
- **reCAPTCHA v3 score tester** — see §3 gotcha (parse the fresh `Result:` line
  only).
- **WAAP identification probe** — fetch the target and classify by the
  headers/cookies/status table at the top of this doc. Always identify the system
  before choosing a strategy.

### Headless vs headed note
Counter-intuitively, **Camoufox in *headless* mode benchmarks higher than headed**
on general bot detectors — its anti-fingerprinting is most complete headless.
Don't assume "headed = more human"; for Camoufox it's the opposite.
