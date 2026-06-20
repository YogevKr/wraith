# AGENTS.md — Driving Wraith as an Agent

> How to point an LLM (or any autonomous loop) at a web page through Wraith: the
> snapshot/index-action perception model, the `AgentBrowser` quickstart, and the
> MCP server. Same ergonomics as **browser-use**, but on top of Wraith's real
> stealth engine — Camoufox + `clear_challenge` + identity borrowing.

**The one-line pitch:** *browser-use gives an agent a clean perception/action
loop; Wraith gives it the same loop on a browser that actually gets past
Reblaze/Link11, reCAPTCHA-v3, Akamai and friends — because it borrows a warmed
identity instead of fighting reputation head-on.*

---

## 0. The mental model — perceive, then act by index

An LLM cannot reason over raw HTML (too big, too noisy) or over pixels alone
(no stable handles). The browser-use insight Wraith adopts:

1. **Perceive** — walk the live DOM, keep only the *interactive* elements
   (links, buttons, inputs, anything clickable/typeable/role-tagged), and give
   each one a small **integer index**. Stamp that index onto the element in the
   page itself (`data-wraith-index="<i>"`). Render the result as a compact,
   one-line-per-element text the model can read.
2. **Act by index** — the model says "click 12" or "type 'BCN' into 4". Wraith
   resolves the index back to the exact element via
   `page.locator('[data-wraith-index="12"]')` and performs the action. No CSS
   selectors, no XPath, no coordinates for the model to hallucinate.
3. **Re-perceive** — every action returns a *fresh* `Snapshot`. Indices are
   only valid for the snapshot that produced them; after any action (or scroll,
   or navigation) you get a new numbering. **Never reuse an index across
   snapshots.**

This is the entire contract. Everything below is sugar on top of it.

---

## 1. The snapshot — `wraith.snapshot`

```python
from wraith.snapshot import take_snapshot, Snapshot, Element

snap = take_snapshot(page, viewport_only=True, highlight=False, max_elements=200)
print(snap.to_text())
```

`take_snapshot(page, *, viewport_only=True, highlight=False, max_elements=200)`
runs a self-contained `buildDomTree` walk inside the page (via
`page.evaluate`). It finds interactive elements —
`a, button, input, select, textarea, [contenteditable], [onclick], [tabindex]`,
plus anything with an interactive ARIA `role`
(`button, link, checkbox, menuitem, tab, switch, radio, combobox, textbox`) —
that are **visible** (and inside the viewport when `viewport_only=True`),
assigns each a sequential integer index, **sets `data-wraith-index="<i>"`** on
the node so it can be acted on later, and returns one `Element` per hit.

### Data shapes

```python
@dataclass
class Element:
    index: int          # the stable handle for this snapshot
    tag: str            # 'button', 'input', 'a', ...
    role: str           # ARIA / implicit role
    text: str           # visible/accessible label
    attributes: dict    # id, name, type, placeholder, href, aria-label, ...

@dataclass
class Snapshot:
    url: str
    title: str
    elements: list[Element]
    screenshot: bytes | None   # populated only when highlight=True

    def to_text(self) -> str: ...          # browser-use-style render (below)
    def by_index(self, i: int) -> Element | None: ...
```

### `to_text()` — what the model actually reads

One line per interactive element, index in brackets, with surrounding
non-interactive context shown **without** an index (so the model can read
labels/headings but only act on the numbered things):

```
Page: Flight Search — example air
[0]<button role=button>Menu</button>
[1]<a role=link>Sign in</a>
Book a flight
[2]<input role=textbox placeholder=From>
[3]<input role=textbox placeholder=To>
[4]<button role=button>Search</button>
[5]<input role=checkbox>Direct flights only</input>
```

The model replies with an action like *"type 'TLV' into 2, type 'BCN' into 3,
click 4"*, and your loop maps each to a Wraith call.

### Knobs

- `viewport_only=True` (default) keeps the snapshot small and matches what a
  user can actually see; set `False` to index the whole document (heavier).
- `highlight=True` draws labeled boxes over each indexed element and returns a
  screenshot in `Snapshot.screenshot` — useful for a multimodal model or for
  debugging *what the indices point at*.
- `max_elements` caps the count so a pathological page can't blow the context
  window.

> **Index lifetime:** an index identifies an element only within the snapshot
> that minted it. The `data-wraith-index` attributes are overwritten on the
> next `take_snapshot`. Always act on the latest snapshot.

---

## 2. `AgentBrowser` — the quickstart

`wraith.agent.AgentBrowser` wraps a Wraith `Session` and exposes the
perceive/act loop as plain methods. Every method that changes the page returns
a fresh `Snapshot` (and stores it as `.last_snapshot`).

```python
from wraith.agent import agent_browser

with agent_browser(engine="camoufox", headless=True) as ab:
    # navigate() runs clear_challenge (passes WAAP), auto-dismisses cookie
    # banners, waits for load, and returns the first snapshot.
    snap = ab.navigate("https://www.example.com/")
    print(snap.to_text())

    # The model picks indices off snap.to_text(); your loop calls these:
    ab.type(2, "TLV")              # fill an input, re-snapshot
    ab.type(3, "BCN", enter=False)
    snap = ab.click(4)             # click "Search", re-snapshot

    print(ab.read())               # readable markdown of the result page
    ab.screenshot("results.png")   # bytes also returned
```

Or reuse a Session you already configured (proxy, borrowed identity, etc.):

```python
import wraith
from wraith.agent import AgentBrowser

with wraith.browser(engine="camoufox", geoip=True) as s:
    # borrow a warmed identity first (see §5)
    cookies = wraith.extract_cookies(wraith.find_zen_profiles()[0],
                                     domain_filter="example.com")
    wraith.inject_cookies(s.context, cookies)

    ab = AgentBrowser(session=s)   # reuses s; will NOT close it (we don't own it)
    ab.navigate("https://www.example.com/account")
    ...
```

### Constructor & ownership

`AgentBrowser(session=None, *, engine="auto", **launch_kw)`

- Pass a `session=` to reuse an existing `Session` — `AgentBrowser` will **not**
  close it (you own it).
- Omit it and `AgentBrowser` lazily launches one via `engine.launch(engine,
  **launch_kw)` and **does** close it on `close()` / context exit.

### Methods

| Method | What it does | Returns |
| --- | --- | --- |
| `navigate(url)` | `clear_challenge(url)` (pass any WAAP), auto-dismiss cookie/consent banners (clicks buttons matching `/accept\|agree\|got it\|מאשר\|אישור\|I understand/i`), wait for load, snapshot | `Snapshot` |
| `snapshot(**kw)` | `take_snapshot(page, **kw)`, store as `.last_snapshot` | `Snapshot` |
| `click(index)` | click `[data-wraith-index="index"]`, re-snapshot | `Snapshot` |
| `type(index, text, *, clear=True, enter=False)` | fill/`human_type` the field, optional Enter, re-snapshot | `Snapshot` |
| `scroll(direction="down", amount=700)` | scroll, re-snapshot | `Snapshot` |
| `read()` | readable markdown of the page (markdownify on `page.content()`, else `inner_text('body')`) | `str` |
| `get_text(index=None)` | text of one element, or the whole page | `str` |
| `screenshot(path=None)` | PNG bytes (saved to `path` if given) | `bytes` |
| `current_url` / `current_title` | properties | `str` |
| `close()` | close the Session **iff** we launched it | — |

`AgentBrowser` is a context manager; `agent_browser(**kw)` is the module-level
contextmanager shortcut.

### A minimal model loop

```python
from wraith.agent import agent_browser

def run(model, goal, start_url):
    with agent_browser(engine="camoufox") as ab:
        snap = ab.navigate(start_url)
        for _ in range(20):
            action = model.decide(goal, snap.to_text())   # your LLM call
            if action.kind == "done":
                return ab.read()
            if action.kind == "click":
                snap = ab.click(action.index)
            elif action.kind == "type":
                snap = ab.type(action.index, action.text, enter=action.enter)
            elif action.kind == "scroll":
                snap = ab.scroll(action.direction)
        return ab.read()
```

---

## 3. Wraith's edge over browser-use

browser-use is excellent at the *agent ergonomics* — the indexed-DOM snapshot,
the act-by-index loop. Where it (and any vanilla-Playwright-based driver) falls
down is the moment a target runs a real anti-bot stack: a fresh Chromium
context trips the fingerprint cluster, gets a `247`/challenge or a
reCAPTCHA-v3 score of ~0.1, and the agent is stuck staring at an interstitial.

Wraith keeps the *same* ergonomics and fixes exactly that layer:

| Concern | browser-use (vanilla Playwright) | Wraith |
| --- | --- | --- |
| Perception | indexed DOM snapshot | same model (`Snapshot.to_text()`, `data-wraith-index`) |
| Action | act by index | same (`click(i)`, `type(i, ...)`) |
| Engine | Chromium (Chrome detection cluster runs) | **Camoufox/Firefox primary** — `isChrome()` branch is false, the whole Chrome cluster is skipped (`window.chrome`, `HeadlessChrome` UA, `$cdc_`/`__driver_` leaks) |
| JS challenges | you hit the interstitial | `navigate()` runs **`clear_challenge`** — solves Reblaze `ac_v2` cookie-free, raises typed errors for IP-tier (`WaapRateLimitedError`) vs hard block (`WaapHardBlockError`) |
| Reputation gates (reCAPTCHA-v3, warmed-account) | unwinnable cold (~0.1 score) | **identity borrowing** — inject a warmed profile's cookies and skip the gated step (see §5) |
| Cookie/consent walls | manual | `navigate()` auto-dismisses common banners (English + Hebrew) |

Put plainly: **same agent loop, a browser that's actually allowed in.** See
[`docs/DETECTION.md`](DETECTION.md) for how to identify each defense and
[`docs/PLAYBOOK.md`](PLAYBOOK.md) for the per-layer strategy
(`247` = engine-solved cookie-free; reputation gate = borrow; `474/481` =
rotate a residential proxy).

---

## 4. The MCP server — `wraith mcp`

Wraith ships an MCP (Model Context Protocol) server so an MCP-aware client
(Claude Code, Claude Desktop, etc.) can drive a stealth browser as a set of
tools. It's built on `FastMCP` and keeps a single lazily-created `AgentBrowser`
behind the tools, so a sequence of `navigate` → `snapshot` → `click` calls
operates on one persistent session.

### Tools exposed

| Tool | Signature | Returns |
| --- | --- | --- |
| `navigate` | `navigate(url)` | snapshot `to_text()` |
| `snapshot` | `snapshot()` | snapshot `to_text()` |
| `click` | `click(index: int)` | new snapshot text |
| `type_text` | `type_text(index: int, text: str, enter: bool=False)` | new snapshot text |
| `scroll` | `scroll(direction: str="down")` | new snapshot text |
| `read` | `read()` | readable markdown of the page |
| `screenshot` | `screenshot()` | path to the saved PNG |
| `detect_waap` | `detect_waap(url: str)` | list of detected WAAP vendors |
| `borrow` | `borrow(domain: str, profile: str=None)` | injects a warmed identity's cookies from a Firefox/Zen profile into the live context |

Imports inside the tools are lazy, so `import wraith.mcp` works even on a host
that has no browser binary installed.

### Install the deps

The agent/MCP layer adds two runtime deps beyond Wraith's core:

```bash
uv add mcp markdownify     # MCP server + readable-markdown rendering
uv run camoufox fetch      # browser binary (if not already fetched)
```

(`mcp` powers the server; `markdownify` is used by `read()` to turn page HTML
into clean markdown.)

### Run it directly

```bash
uv run wraith mcp          # starts the MCP server (stdio transport)
# equivalently:
uv run python -m wraith.mcp
```

### Add it to Claude Code

The simplest path — register the stdio server with the `claude mcp add` CLI:

```bash
# from the wraith repo (so `uv run` resolves the right env):
claude mcp add wraith -- uv run --directory /Users/yogev/wraith wraith mcp
```

Then `claude mcp list` should show `wraith`, and inside a Claude Code session
the `navigate` / `snapshot` / `click` / ... tools become available. Scope it to
your user config with `-s user` if you want it everywhere, or leave it
project-local (default).

If you installed Wraith into a plain virtualenv instead of uv:

```bash
claude mcp add wraith -- /path/to/.venv/bin/wraith mcp
```

### Add it to Claude Desktop (`claude_desktop_config.json`)

Edit the desktop config — on macOS it lives at
`~/Library/Application Support/Claude/claude_desktop_config.json` (on Windows,
`%APPDATA%\Claude\claude_desktop_config.json`) — and add a `wraith` entry under
`mcpServers`:

```json
{
  "mcpServers": {
    "wraith": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/yogev/wraith", "wraith", "mcp"]
    }
  }
}
```

Restart Claude Desktop; the Wraith browser tools appear in the tools menu. (If
you don't use uv, set `"command"` to the absolute path of the `wraith` console
script in your environment and drop the `uv run --directory` args.)

> The MCP server drives **one** browser session for the lifetime of the
> client connection. Call `navigate` first (it clears any WAAP and dismisses
> consent banners), then perceive/act with `snapshot` + `click`/`type_text`.
> Use `borrow(domain, profile)` *before* navigating into an authenticated area
> to inject a warmed identity.

---

## 5. Borrowing a warmed identity in the loop

The reason an agent on Wraith gets through reputation gates is that it doesn't
arrive cold. Before navigating into anything gated by reCAPTCHA-v3 or a
warmed-account check, borrow the user's live session from a real on-disk
profile:

```python
import wraith
from wraith.agent import AgentBrowser

with wraith.browser(engine="camoufox", geoip=True) as s:
    profile = wraith.find_zen_profiles()[0]          # or find_firefox_profiles()
    cookies = wraith.extract_cookies(profile, domain_filter="example.com")
    wraith.inject_cookies(s.context, cookies)         # warmed identity injected

    ab = AgentBrowser(session=s)
    ab.navigate("https://www.example.com/account")    # already logged in; no gate
    ...
```

Over MCP this is the `borrow(domain, profile=None)` tool. Use **Firefox or Zen**
profiles (same engine family as Camoufox, plaintext cookie store); Chrome
cookies are AES-encrypted via the OS keychain and Wraith deliberately refuses to
decrypt them. See [`docs/PLAYBOOK.md` §4–5](PLAYBOOK.md) for cookie extraction
and live-token harvesting details.

---

## 6. reCAPTCHA v3 — there is no solver

reCAPTCHA **v3 is a reputation score, not a puzzle.** It returns `0.0..1.0`
derived from Google-account cookies, aged history, and IP reputation — set at
the moment the token is **minted**, not by anything the agent does on the page.
A fresh automated profile scores ~0.1–0.3 no matter how clean the engine or how
human the mouse; a real warmed browser scores ~0.9. **You cannot fake it, and
there is no client-side solver.**

So Wraith doesn't ship one. What it ships (`wraith.recaptcha`):

- **`harvest_token(page, sitekey, action="submit", *, timeout=30.0,
  enterprise=False) -> str`** — the *preferred* route. Calls
  `grecaptcha[.enterprise].ready` → `execute(sitekey, {action})` inside the page
  and returns the token string. **Mint it from a warmed or borrowed context**
  (so the score is high), and note the constraints: a v3 token is **single-use**,
  **action-bound**, and good for only **~120s** — harvest it just-in-time, right
  before the request that needs it, and don't try to cache it across actions.
- **`score(target) -> float`** — delegates to `wraith.detect.recaptcha_v3_score`
  so you can read what reputation the current identity actually carries (low =
  switch to identity borrowing; see §5).
- **`SolverService` / `CapSolver` / `TwoCaptcha`** — bring-your-own-key
  skeletons for third-party solving services. Be clear-eyed: these are **cold
  farms** and typically mint ~0.10-score tokens — they do **not** make a bot
  look warmed, and using them may cross a site's terms of service. They exist
  for completeness; the real answer for v3 is **harvest from a warmed/borrowed
  session**, not buy a token.

```python
from wraith import recaptcha

# preferred: mint from a warmed/borrowed context, just before you need it
token = recaptcha.harvest_token(page, sitekey="6Lc...", action="login")

# diagnose the current identity's reputation
print(recaptcha.score(page))   # ~0.1 cold, ~0.9 warmed
```

The throughline of the whole toolkit: **don't beat reputation defenses — borrow
a warmed identity and let the reputation come along for free.**

---

## See also

- [`docs/DETECTION.md`](DETECTION.md) — identify which WAAP/anti-bot system a
  target runs (header/cookie/status cheat sheet + per-vendor deep dives).
- [`docs/PLAYBOOK.md`](PLAYBOOK.md) — the per-layer decision playbook (engine
  choice, the `playwright==1.55` pin, identity borrowing, proxy rotation,
  behavior).
- [`README.md`](../README.md) — install, the `wraith` CLI, and the library
  quickstart.
