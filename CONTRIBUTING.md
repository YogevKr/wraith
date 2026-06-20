# Contributing to Wraith

Thanks for your interest in Wraith — the identity-borrowing stealth browser for
autonomous agents. This guide covers local setup, the module layout, the two
most common extension points (adding a WAAP vendor signature and adding a proxy
provider), and the pull-request norms.

Before contributing, please read the **Responsible Use & Legal** section of the
[README](README.md) and our [Code of Conduct](CODE_OF_CONDUCT.md). Wraith is a
dual-use tool; contributions are accepted on the understanding that the project
is framed around legitimate use (accessing your own accounts/data, authorized
security testing, research, and personal automation).

## Development setup

Wraith uses [uv](https://docs.astral.sh/uv/) for dependency and environment
management.

```bash
# Clone, then from the repo root:
uv sync --extra dev        # core deps + pytest (the test suite)
```

`uv sync` creates and populates `.venv/` from `uv.lock`. You do not need to
activate the venv manually — prefix commands with `uv run`.

The core library (engine, identity, detect, proxy, providers, behavior,
snapshot, agent, harvest, recaptcha, mcp) imports without any browser binary
installed, and the offline test suite runs without one. You only need the
browser binaries to actually drive a page:

```bash
uv run camoufox fetch              # Camoufox Firefox build (primary engine)
uv run patchright install chromium # patched Chromium (fallback engine)
```

### Running the tests

```bash
uv run pytest                      # full suite
uv run pytest -q                   # quiet
uv run pytest tests/test_detect.py # a single file
uv run pytest -k waap              # by keyword
```

The suite is **fully offline**: no network, no browser binaries. It asserts the
package imports cleanly (even on a partial install), the public API symbols
exist, WAAP fingerprinting matches synthetic header/cookie/body fixtures, and
the proxy/provider/agent/recaptcha/mcp surfaces behave. **The suite must stay
green** — `uv run pytest -q` should report `158 passed` or more. New behavior
needs a new test; keep tests offline (use fixtures and duck-typed fakes rather
than live targets, mirroring the existing `tests/`).

### Linting

```bash
uv run ruff check                  # lint
uv run ruff check --fix            # autofix the safe lints
uv run ruff format                 # format
```

Keep the diff `ruff`-clean. Match the surrounding style: `from __future__ import
annotations` at the top of every module, type hints on public functions, and a
module-level docstring that explains *why*, not just *what* (see `detect.py` and
`providers.py` for the house style — the empirically-verified gotchas are
documented inline on purpose; preserve that).

## Module layout

Everything lives under `wraith/`. The modules are small and mostly independent;
`identity`, `behavior`, `harvest`, `detect`, `proxy`, and `providers` are
duck-typed against the Playwright sync API and import with no browser installed.

| Module | Responsibility |
| --- | --- |
| `wraith/engine.py` | Stealth launcher & engine selection. `launch()` / `browser()` return a `Session`; `clear_challenge()` drives the WAAP clearance/proxy-rotation loop. Camoufox primary, patchright Chromium fallback; enforces the `playwright==1.55` pin. |
| `wraith/identity.py` | **Signature feature.** Discover Firefox/Zen/Chrome profiles, `extract_cookies`, normalize, and `inject_cookies` into a context. Firefox/Zen plaintext; Chrome raises `ChromeEncryptionError`. |
| `wraith/harvest.py` | Live session capture. `SessionHarvester` latches the first request carrying an `Authorization` header + auth cookie; `harvest_session()` is the high-level helper. |
| `wraith/detect.py` | Diagnostics + the WAAP signature table (`SIGNATURES`, `Signature`): `identify_waap()`, `fingerprint()`, `recaptcha_v3_score()`, `bot_detector()`, `cookie_is_valid()`, `CLEARANCE_COOKIES`. |
| `wraith/proxy.py` | Dependency-free `ProxyPool` (rotation strategies) + `normalize_proxy`, consumed by `clear_challenge`. |
| `wraith/providers.py` | First-class residential-proxy providers (`DataImpulse`) that build proxy-URL strings / `ProxyPool`s for the engine. |
| `wraith/behavior.py` | Human-like helpers: `human_move`, `human_type`, `dwell`. |
| `wraith/snapshot.py` | Agent perception layer: indexed, browser-use-style DOM snapshots (`take_snapshot`, `Snapshot`, `Element`). |
| `wraith/agent.py` | The perceive/act-by-index browser wrapper (`AgentBrowser`, `agent_browser`) built on the snapshot layer. |
| `wraith/recaptcha.py` | reCAPTCHA-v3 token harvesting (`harvest_token`, `score`) + solver-service skeletons. |
| `wraith/mcp.py` | MCP server (`wraith-mcp`) exposing the agent + diagnostics as tools. |
| `wraith/cli.py` | The `wraith` command. Lazily imports each component so `--help` works on a partial install. |

Public symbols are re-exported from `wraith/__init__.py` via the defensive
`_reexport` helper: a missing optional dependency is recorded in
`wraith.missing_imports` rather than breaking `import wraith`. **When you add a
public symbol, add it to its module's `__all__` and to the matching `_reexport`
list in `wraith/__init__.py`.**

Runnable, commented examples live in `examples/`; deep-dive references live in
`docs/` (`DETECTION.md`, `PLAYBOOK.md`, `AGENTS.md`).

## How to add a WAAP vendor signature

The WAAP layer is driven by a single source of truth: the `SIGNATURES` tuple in
`wraith/detect.py`. Detection, tiering, the pass strategy, and the clearance
cookies all come from one `Signature` dataclass per vendor — you should not need
to touch the matching engine.

1. **Add a `Signature` entry** to `SIGNATURES` (in `wraith/detect.py`). Order
   matters: it determines the stable order of `identify_waap`'s output, so place
   heavyweight WAAPs before CDN/edge ones before CAPTCHA widgets / IAM gateways.
   Populate only the signals you actually have evidence for — detection is the
   OR of every populated field, matched case-insensitively:

   ```python
   Signature(
       name="ExampleShield",          # canonical name identify_waap returns
       tier=2,                        # 1 engine passes / 2 smarter / 3 solver|IAM
       strategy="real engine clears the JS challenge; warmed identity helps",
       clearance_cookies=("es_clear",),   # cookies that mark a cleared session
       headers=("x-example-shield",),     # header names whose presence flags it
       header_contains=(("x-cdn", "exampleshield"),),  # {header: substring}
       server=("exampleshield",),         # substrings in the Server header
       header_substr_any=("exampleshield",),  # substring vs ANY header name/value
       cookies=("es_id",),                # cookie names whose presence flags it
       cookie_prefixes=("es_sess",),      # match cookies starting with a prefix
       body=("static.exampleshield.com", "es-challenge"),  # body substrings
       statuses=(499,),                   # non-standard status codes
   ),
   ```

2. **Pick the tier honestly.** Tier 1 = a good stealth engine alone passes;
   tier 2 = needs a behavioral nudge / warmed identity / retries; tier 3 = a
   human-grade solver or IAM credential is required and no cookie-poll clearance
   is possible from automation alone (use empty `clearance_cookies`). The tier
   tells the rest of Wraith how to treat the vendor.

3. **JS-only globals:** if the vendor's tell only appears after its script runs,
   add the global to `_PAGE_JS_GLOBAL_PROBE` and map it to a `body` substring the
   signature already looks for (so a live-page hit lands on the right vendor
   without growing the schema).

4. **Special-case clearance validity:** if mere cookie *presence* is not enough
   (as with Akamai `_abck`, where the value carries the `~0~`/`~-1~` verdict),
   extend `cookie_is_valid()`.

5. **Add a coverage test** in `tests/test_waap_coverage.py`: a synthetic
   header/cookie/body/status fixture that your signature should match, plus an
   assertion it does not false-positive on unrelated input. Update the tier
   matrix in `docs/DETECTION.md` to match.

## How to add a residential-proxy provider

Providers (in `wraith/providers.py`) turn an account into proxy-URL strings and
`ProxyPool`s that feed `engine.launch(proxy=...)` and
`clear_challenge(proxy_pool=...)`. Follow the `DataImpulse` shape:

1. **Construction must never raise.** Resolve credentials most-specific-first
   (explicit args > environment variables > optional `~/.secrets` file via
   `_parse_secrets_file`) and store unresolved ones as `None`. Raise a provider
   `*AuthError` *lazily*, only when a method that actually needs credentials is
   called — so `import wraith` and a bare constructor stay safe with no account.

2. **Expose the standard surface:** `rotating(...)` (new IP per request),
   `sticky(session_id, ...)` (IP pinned for the session lifetime), and
   `pool(n, ...) -> ProxyPool` (n distinct sticky exits for rotation). Build
   `http`/`https`/`socks5` URLs of the form
   `scheme://user:pass@host:port`.

3. **Document geoip/identity consistency.** Note in the docstring that the
   provider pairs with `geoip=True` so Camoufox derives a coherent
   timezone/locale from the exit IP.

4. **Wire it in:** add the class + its `*AuthError` to the module `__all__` and
   to the `providers` `_reexport` list in `wraith/__init__.py`. Add the CLI flag
   if it should be reachable from the `wraith` command (see how `cli.py` wires
   DataImpulse).

5. **Add tests** in `tests/test_providers.py`: URL assembly, username
   enrichment, lazy auth error, and pool construction — all offline, no network.

## Pull-request norms

- **Branch & scope.** Work on a feature branch off `main`; keep each PR focused
  on one change. Don't reformat unrelated code in the same diff.
- **Commits.** Use Conventional Commits, matching the existing history, e.g.
  `feat(waap): add ExampleShield signature`, `fix(engine): ...`,
  `docs: ...`, `test: ...`.
- **Green before review.** `uv run pytest -q` (158+ passing) and
  `uv run ruff check` must both pass. Add tests for new behavior; keep them
  offline.
- **Docs.** Update the relevant `docs/` reference and the README when you change
  user-facing behavior or add a vendor/provider.
- **Never commit secrets.** No real credentials, cookies, harvested sessions,
  `*.session.json` files, or proxy passwords. Use synthetic fixtures in tests.
- **No invented dependencies.** Do not add a runtime dependency that isn't
  already in `pyproject.toml` without discussing it in an issue first.
- **Privacy.** This project keeps personal email out of the codebase and
  metadata. Don't add email addresses; reference
  <https://github.com/YogevKr/wraith> for contact.

Found a security issue instead of a feature? Please follow
[SECURITY.md](SECURITY.md) rather than opening a public issue or PR.
