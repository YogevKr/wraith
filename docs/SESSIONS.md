# Sessions: Borrow, Sync, and Hand Off

Wraith can run as a user who is already signed in. This page covers the
session tools: borrowing your own profile, syncing a login to a remote
machine, handing a session to agent-browser, and the managed Clawd flow.

## Borrow a warmed identity in Python

Launch a session and borrow a warmed identity from your own profile.

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

> The base `borrow` / `extract_cookies` path reads **Firefox/Zen** profiles
> (plaintext cookie store, same engine family as Camoufox) and still raises
> `ChromeEncryptionError` for Chrome, on purpose. To use a **Chrome** login,
> reach for `wraith profile sync --from chrome` below — it opts into the OS
> keychain decryptor (`wraith.chrome`) explicitly.

A borrowed cookie keeps its CHIPS partition key (top-level site) when the
source browser stored one: Firefox `originAttributes` and Chrome
`top_frame_site_key` both normalize to Playwright's `partitionKey`. Camoufox
ignores the key and still sends the cookie; Chromium stores the partitioned
cookie.

## Profile sync — a laptop login on a remote Wraith

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
  [`deploy/`](../deploy/) with `wrangler deploy` — no KV namespace.
- **Revoke**: holding the secret also lets you burn a drop —
  `wraith profile revoke <code>` deletes it at the relay if you mis-sent it or
  the code leaked.
- **Over Tailscale instead?** Skip the relay: `ssh <host> wraith profile receive <code>`.

A cookie jar is the session past 2FA — keep syncs domain-scoped, and see
[SECURITY.md](../SECURITY.md#profile-sync-dead-drop) for the trust limits.

## Hand off a session to agent-browser

[agent-browser](https://github.com/vercel-labs/agent-browser) is a Chromium
automation CLI for agents. It has a wide command set but no stealth engine. Use
Wraith to get the session, then give the session to agent-browser.

Firefox (Camoufox) exports a cookie without a SameSite attribute as
`sameSite: "None"`. Chromium drops a `None` cookie that is not Secure. The
Chromium export changes each such cookie to `"Lax"`, the Chromium default.

```bash
# Borrow your own login and save a Chromium-ready state file.
# Headed mode waits for Enter, so you can finish a login in the window first.
uv run wraith borrow https://app.example.com --save-state app.state.json --for-chromium
agent-browser --state app.state.json open https://app.example.com
```

```python
from wraith import agent_browser

with agent_browser(engine="camoufox") as ab:
    ab.navigate("https://app.example.com")
    ab.save_storage_state("app.state.json", for_chromium=True)
```

From MCP, call `save_state(path, for_chromium=True)`.

Limits:

- agent-browser does not hide automation. On the rebrowser bot detector it
  fails `runtimeEnableLeak` and `navigatorWebdriver`. A site with bot checks
  can challenge the Chromium session again.
- Some clearance cookies are bound to the browser fingerprint or the exit IP,
  for example Cloudflare `cf_clearance`. They can fail in a different browser.
  Use the handoff where a login cookie controls access, not a bot challenge.
- Do not connect agent-browser to a Wraith browser over CDP. Camoufox has no
  CDP endpoint. On Wraith's Chromium, agent-browser sends `Runtime.enable`, and
  the `runtimeEnableLeak` check fails.
- The file contains session tokens. Keep it private and delete it after use.
  Export is blocked after a secret fill unless you pass `allow_secret_tainted=True`.

## Managed Clawd flow

`wraith clawd-run --config flow.json` validates a versioned flow and prints a
plan. Add `--apply` to run the Tailnet, exit check, and vault canary commands.
Add `--execute` only when the final configured browser command is approved.
Commands use JSON argv arrays. Version 2 flows must include an
`exit.identity_command` that returns `tailscale status --json` and an
`exit.identity_expected` value. Wraith requires `BackendState` to be `Running`,
`ExitNodeStatus.Online` to be true, and the expected device name, DNS name, ID,
or Tailscale IP to match. A public IP match alone cannot prove the exit device.
Wraith captures command output and returns status only, so vault values do not
enter logs.
