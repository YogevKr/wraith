# Changelog

All notable changes to **Wraith** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.19.0] - 2026-10-25

### Added

- Borrowed cookies keep their CHIPS partition key. Firefox
  ``originAttributes`` (``(scheme, domain)``) and Chrome
  ``top_frame_site_key`` both normalize to Playwright's ``partitionKey`` on
  injection. Chrome extraction adapts when an older DB has no
  ``top_frame_site_key`` column. Camoufox ignores the key and still sends the
  cookie; Chromium stores the partitioned cookie.
- ``clear_challenge(..., require_clearance=True)``. A clean 200 with content no
  longer counts; only a valid clearance cookie returns. Use it when a caller
  needs a truly cleared session (for example Akamai ``_abck``, where presence
  is not validity).
- ``Session.humanize``. When a caller enables Camoufox ``humanize``, Wraith
  skips its own ``human_move`` before a click, so the cursor does not travel
  two human paths.

## [0.18.1] - 2026-10-25

### Fixed

- A navigation that starts late no longer slips past the agent's wait. The
  start window was 500 ms; a slow machine or a delayed JS navigation could
  start after it, so `click` / `type(enter=True)` returned before the page
  committed and a follow-up `url` read saw the old page. The window is now
  1500 ms. The macOS live suite caught this on a slow runner.

## [0.18.0] - 2026-10-25

### Changed

- `clear_challenge` no longer accepts a 200 page that still looks like a
  Cloudflare challenge wall (markers such as `challenge-platform` or
  "Just a moment") while no clearance cookie exists. It waits for the
  challenge JS to finish instead of returning early.
- `new_tab`, `go_back`, and `go_forward` now run the same WAAP clearance as
  `navigate`. `new_tab` routes through `clear_challenge`; the history moves use
  a new `navigate=False` mode that polls the current page in place.
- Added `dblclick(index)` and the MCP `double_click` tool. Two `click` calls do
  not fire `dblclick`; the new path does.
- Consent-button clicks (`_dismiss_consent`) and indexed clicks inside a frame
  now travel a human cursor path first, like other clicks.

## [0.17.0] - 2026-10-25

### Changed

- Camoufox updated to `0.5.9b5` and Playwright to `1.62`, with the Camoufox
  browser it pairs with (`158.0-beta.40`). The Firefox identity is now 158,
  not 152. Camoufox 0.5.6 fixed the 0.4.x `pageError` crash, so the old
  `playwright < 1.60` ceiling no longer applies. `camoufox fetch` installs the
  paired build.
- The fast path replays Camoufox with that build's own TLS hello. Firefox 156
  dropped the `49162` cipher and the `256-257` curve from its ClientHello, so
  `_CAMOUFOX_JA3` was retuned. JA3, JA4, peetprint, and the HTTP/2 fingerprint
  were verified equal to the browser against `tls.peet.ws` (156 and 158 send
  the same hello).
- A persistent profile now keeps one Camoufox device fingerprint. Camoufox
  draws a new random device every launch, so a reused profile changed the
  device while the cookies stayed. The chosen preset is stored beside the
  profile and reused. A caller fingerprint or preset still wins.
- `clear_challenge` now clicks a visible Cloudflare Turnstile checkbox. The
  widget is cross-origin, so it aims at the checkbox fraction of the widget
  frame. Solves 3 of 3 on `nowsecure.nl`; a site that needs the token no longer
  waits for the agent to aim by hand.
- The behavioral nudge now sends one `wheel` notch (down, then back up), not
  just cursor moves, so a sensor sees the input channel a hand uses.
- An indexed `click` now travels a human cursor path to a random point near the
  element center, not the exact center, and falls back to the center when a
  point would miss.
- Chromium keeps WebGL available on a headless host with no GPU by forcing
  ANGLE/SwiftShader, instead of leaving WebGL null. A caller GL choice wins.
- A SOCKS proxy now sets `network.proxy.socks_remote_dns`, so DNS resolves at
  the exit instead of leaking the local resolver.
- The Chromium fallback now honors `geoip=True`: it derives the exit IP's
  locale and timezone through Camoufox's GeoIP data instead of advertising
  the host's. A proxied Chromium run no longer contradicts its own exit IP.
- Chromium now passes `--webrtc-ip-handling-policy=disable_non_proxied_udp`
  when a proxy is set, so WebRTC cannot expose the host's real IP next to a
  proxy exit IP. A caller-supplied policy still wins.
- Relative `scroll` (up/down/left/right) now uses real `wheel` events in
  several small notches instead of one `window.scrollBy`, so behavioral
  sensors see a human scroll stream. It falls back to `scrollBy` when a page
  swallows the wheel. `top`/`bottom` still use `scrollTo`.

## [0.16.1] - 2026-10-18

### Fixed

- `wait_for` no longer closes the browser when its text does not show. On
  Python 3.11 and later, the MCP server read the tool's own `TimeoutError`
  as a stuck browser: it reported "BrowserStuck: the browser did not answer
  within 180 s" at once and closed the browser, so the agent lost its pages,
  tabs, and login. The tool now returns "TimeoutError: '…' did not appear
  within N s" and keeps the browser. `wraith call wait_for` had the same
  fault. The 180 s watchdog still closes a browser that does not answer.
- The live browser tests in CI stop when one test runs past 240 s. A hung
  browser call printed its stack at 240 s but then held the job until the
  25-minute timeout.

## [0.16.0] - 2026-10-18

### Added

- A no-browser replay now says when its TLS hello can differ from the
  browser the session came from. A session from Chrome 155 replays with
  curl_cffi's `chrome146` hello: Chrome 155 sends TLS extension 51764, and
  no curl_cffi preset sends it, so a site that ties clearance to the TLS
  fingerprint can challenge the replay. `wraith fetch` prints the note on
  stderr, the MCP `fetch` tool adds a `Note:` line, and
  `wraith.fastpath.fetch()` raises a `TLSMismatchWarning` (or calls
  `on_tls_mismatch`). `wraith.fastpath.tls_note()` returns the text. Sessions
  from Camoufox replay with their own browser's hello and get no note.

## [0.15.0] - 2026-10-18

### Added

- `wraith call TOOL ...` runs any MCP tool from a shell, with the same
  arguments: `wraith call navigate URL`, `wraith call click 3`,
  `wraith call read --find invoice`. The calls share one browser in a
  background session, so the page, its cookies, and the element indices carry
  over from one command to the next. Before, an agent without MCP wrote a
  script for each step, and each script started a new browser.
- `wraith session start|stop|status` controls those sessions. The first
  `wraith call` starts the session `default` with the MCP server's defaults.
  `session start` takes `--engine`, `--headed`, the proxy flags,
  `--allowed-domains`, and `--upload-dir`. A session stops after 30 minutes
  with no call (`--idle-timeout`).
- Only your user can connect to a session: its Unix socket is in
  `~/.wraith/sessions` (mode 0700; `WRAITH_SESSION_DIR` changes it). The
  launch options reach the session on stdin, so a proxy password does not
  show in `ps`.

### Fixed

- A `wraith` command whose output goes to a closed pipe (`| head`) no longer
  prints a `BrokenPipeError` traceback.

## [0.14.3] - 2026-10-18

### Fixed

- `clear_challenge` (and `navigate`) no longer reports a hard block for a
  HUMAN Press & Hold challenge that a hold just cleared. Firefox can deliver
  the cleared page's commit before the redirect's response event, so the
  "access denied" text read before the hold looked current. For 5 seconds
  after a poll that saw the challenge, that text is not a hard block; a
  denied page that stays after that still fails fast. CI hit this twice in
  the live Press & Hold tests.

## [0.14.2] - 2026-10-17

### Changed

- The README is shorter and easier to scan: a banner, a snapshot example,
  measured stealth and challenge results, a three-step quickstart, the MCP
  tools by group, a configuration table, and diagrams for the defense tiers,
  profile sync, and the modules. The secret-fill reference moved to
  `docs/SECRETS.md`; borrow, profile sync, agent-browser handoff, and the
  Clawd flow moved to `docs/SESSIONS.md`.

### Fixed

- The README told users to run `pip install wraith`, which installs a
  different project from PyPI. It now installs from GitHub. The CLI and fast
  path error messages no longer point at PyPI, and the CLI no longer names a
  `wraith[all]` extra that does not exist.

## [0.14.1] - 2026-10-17

### Fixed

- `click_frame(frame, x, y)` aims in the frame's own coordinates. Before, the
  fractions applied to the frame's on-screen box, which is larger than the
  frame when the frame is rotated or tilted in 3D, so the click missed. Wraith
  now reads where the cursor lands inside the frame and corrects the aim with
  Newton steps before it clicks. A 40 px button in a 3D-tilted frame: 0 of 3
  hits before, 3 of 3 after, on Camoufox and Chromium.

## [0.14.0] - 2026-10-17

### Added

- `read(find="population")` (MCP) and `AgentBrowser.find(term)`: the passages
  of the whole page that contain the words, each clipped around the match; a
  table row comes with the rows that follow it. On the Wikipedia "Tel Aviv"
  article it returns `| Population (2024) | | • City | 494,900 |` and the
  lead's "with a population of 494,900" in one call.

### Changed

- `read()` returns the main content by default (the first of `<main>`,
  `[role=main]`, `<article>`, `#content` that holds real text); `full=True`
  returns the whole page.
- `read()` renders links and images as their text unless `links=True`. Link
  URLs more than doubled a page: the Tel Aviv article was 334,002 characters
  (17 slices) and is now 138,524.

## [0.13.3] - 2026-10-17

### Fixed

- One stuck browser call no longer freezes the MCP server. A page can block a
  Playwright call that has no timeout (during dogfooding, `navigate` hung and
  every later tool waited behind it on the single browser worker). Each tool
  call now has a deadline (`WRAITH_TOOL_TIMEOUT`, default 180 s). When it
  passes, the tool reports `BrowserStuck`, Wraith kills that browser's
  Playwright driver and its child processes, and the next call starts a new
  browser on a new worker. Open pages and tabs are lost.

## [0.13.2] - 2026-10-17

### Fixed

- Camoufox pages no longer switch language between launches. With `geoip`,
  Camoufox draws a random language for the IP's country on each launch (for
  an Israeli IP: Hebrew, English, Arabic, Russian, and more), so Cloudflare
  showed its widget in Arabic in one run and Hebrew in the next. On a direct
  connection with no `locale` given, Wraith now uses this computer's locale
  (`LC_ALL`, `LC_MESSAGES`, `LANG`, then macOS `AppleLocale`): stable, and what
  its real browsers show on the same IP. With a proxy, the proxy country's
  language draw stays.

## [0.13.1] - 2026-10-17

### Fixed

- Snapshots show what a control holds: a `<select>` shows its chosen option as
  its text and its options as `options="…"` (it showed every option's text
  run together), a file input shows `files="…"`, and range and colour inputs
  show their value. Typed text fields still never show a value.
- The `Changed:` line after an action names what changed: new visible text
  (`new text: "File Uploaded!"`) and new control values (`country -> Israel`).
  It said "no visible change detected" after `select_option` and needed a
  `read()` to confirm a form submit.
- `upload_file` refuses a link, a text field, or a `<select>` before it
  clicks anything. With a wrong index it clicked a link (a new tab opened) and
  then failed with a raw Playwright timeout. A trigger that opens no file
  chooser now says so.

### Added

- Snapshots end with `Challenges: turnstile solved` (or `not solved yet`;
  also reCAPTCHA and hCaptcha) when the page has a captcha token field. Only
  the field's state is read, never the token.

## [0.13.0] - 2026-10-17

### Added

- `drag(dx, dy=0, index=… | x=…, y=…)` (`AgentBrowser` and MCP): press on a
  knob, move it along a human path, release. The path eases out, wobbles a
  little, has uneven event timing, overshoots long drags by 2–6% and settles,
  and the button is held before the move. `wraith.behavior.drag_path` and
  `human_drag`. With the distance read from `screenshot()`, it passed GeeTest
  v3 and v4 slide puzzles and Lemin's puzzle on their public demo pages.

### Changed

- The macOS live CI job stops after 25 minutes, prints each test name, and
  dumps the stack of a test that runs over 4 minutes. A hung run held a
  runner for over 35 minutes with no output.

## [0.12.5] - 2026-10-17

### Fixed

- An action that took longer than 500 ms (a human-like `click_at` or
  `click_frame`, or any click on a slow machine) could return before the page
  it opened committed. Playwright's `expect_request` timer starts when the wait
  is created, so it ran out during the action. The 500 ms window now starts
  when the action ends.
- `clear_challenge` could report a hard block on a Press & Hold page that was
  clearing: when the cleared page answered while the hold check ran, the
  content read before it (still "access denied") counted as a block. A poll
  during which the main frame navigated no longer decides a block.

## [0.12.4] - 2026-10-17

### Added

- The stealth benchmark keeps the bot detector's own text for each check that
  warns or fails, and `wraith benchmark compare` prints it under the table.
  `wraith.detect.bot_detector_notes(page, names)` reads it.

### Documentation

- The playbook explains the Linux `bot.headless` warning: without a GPU,
  Chrome draws WebGL with SwiftShader, which only headless Chrome does. Use
  Camoufox on such hosts.

## [0.12.3] - 2026-10-17

### Fixed

- The fast path replays a session from Wraith's pinned Camoufox (User-Agent
  `Firefox/152.0`) with Camoufox's own TLS hello. It used curl_cffi's
  `firefox147` preset: one more cipher (`c00a`), so a server saw a different
  client than the one that earned the cookies. Now the JA3, JA4, peetprint,
  HTTP/2 fingerprint, and key shares match. An explicit `ja3`, `akamai`, or
  `extra_fp` still wins.

### Added

- Benchmark check `replay.matchesBrowser`: the fast path, replaying with the
  browser's User-Agent, sends the browser's JA4 and HTTP/2 fingerprint. It
  passes for Camoufox. It fails for Chromium: Chrome 155 sends an extension
  (51764) that curl_cffi 0.15 cannot send.

## [0.12.2] - 2026-10-17

### Fixed

- `select_option` chooses like a keyboard user: it focuses the list and types
  the option's label, so the page gets trusted key, `input` and `change`
  events. Playwright's `select_option` fired `input` and `change` with
  `isTrusted=false` in Chromium. It is still the fallback for a `multiple`
  list, a label with a character that has no key, or two options with one
  label.

## [0.12.1] - 2026-10-17

### Fixed

- Chromium contexts no longer emulate Playwright's 1280x720 viewport and
  screen. In Playwright for Python, `viewport=None` is the same as leaving it
  out, so Wraith's `viewport=None` kept the emulation. Headed Chromium
  reported a 1280x720 screen inside a 1282x800 window; it now reports the real
  screen and window. Wraith uses `no_viewport=True`, and the docs say so.

### Added

- The live fingerprint check runs headed Chromium too.

## [0.12.0] - 2026-10-17

### Added

- Network log: `AgentBrowser.network(contains, resource_type, limit)` lists the
  last 500 requests of the browser context (method, URL, type, status, content
  type, failure), and `AgentBrowser.network_response(request_id)` returns a
  response body. MCP tools `network_requests` and `network_response` (in
  slices, like `read`). Use them to find a site's JSON API.
- After a secret fill, response bodies are refused and URLs lose their query
  strings. The log keeps no request headers or request bodies.

## [0.11.0] - 2026-10-17

### Added

- `wraith benchmark run`: one JSON record per engine with the bot-detector
  checks, the browser's TLS and HTTP/2 checks, and the same network checks for
  the no-browser fast path. `wraith benchmark compare` lists checks that passed
  in the previous run and now warn or fail, and JA4 or HTTP/2 fingerprints that
  changed.
- `wraith.detect.network_fingerprint(page)` and `network_checks(data)`: TLS
  1.3, HTTP/2, no `HeadlessChrome` in the header, and an HTTP/2 fingerprint
  (pseudo-header order and SETTINGS) that matches the browser family the
  User-Agent claims.
- Weekly workflow `stealth benchmark` on macOS and Linux for Camoufox and
  Chromium. It compares with the previous successful run on `main` and opens or
  updates a `stealth-regression` issue. Unmeasured (`unknown`) checks never
  open one.

## [0.10.0] - 2026-10-17

### Added

- `press_key(key)`: press Escape, Tab, arrow keys, Enter, or a chord on the
  focused element. The key is held for a human time.
- `select_option(index, option)`: choose a `<select>` option by value or
  label. An unknown option fails with the option list.
- `upload_file(index, paths)`: put files into a file input, or click an upload
  button and fill its file chooser. Uploads are off until folders are allowed
  with `AgentBrowser(upload_dirs=...)`, `wraith mcp --upload-dir`, or
  `WRAITH_UPLOAD_DIRS`. `wraith.UploadPolicy` and `wraith.UploadNotAllowedError`.
- `go_back()`, `go_forward()`, and `wait_for(text, seconds)`.
- All six are MCP tools too.

### Changed

- The MCP `read` tool returns a long page in slices of 20,000 characters
  (`read(start, max_chars)`). The last line names the next `start`.
- The MCP server sends stray `print` output to stderr. A print reached the
  stdio channel as invalid JSON, even with MCP SDK 2.1.1.

## [0.9.2] - 2026-10-17

### Fixed

- Snapshots no longer write `data-wraith-index` attributes into the page. A
  page could find the elements an agent indexed, and the name Wraith, with
  `document.querySelector('[data-wraith-index]')` or a `MutationObserver`.
  Wraith now keeps index → element in a `Map` in its isolated JavaScript world,
  which page scripts cannot read, and resolves indices through it.
- Secret fills no longer mark the field with `data-wraith-secret`. Nothing read
  the mark, and it told the page which field held a secret.
- Snapshots no longer scan every element of the page to remove old marks.

### Added

- The live input check fails when Wraith adds an attribute to a page node, or
  when the page can see a Wraith global.

## [0.9.1] - 2026-10-17

### Fixed

- Headless Chromium no longer says `HeadlessChrome` in its User-Agent, worker
  User-Agent, request headers, or Client Hints brands. `wraith selftest
  --engine chromium --headless` failed the `useragent` and `headless` checks.
- The Chromium engine uses an installed Google Chrome when it finds one, and
  full Chromium (`channel="chromium"`) otherwise in headless mode. The bundled
  headless shell had no WebGL and no `window.chrome`.
- Headless Chromium windows are sized like a maximized 1920x1080 window, not
  Playwright's `1280x720` default.
- Chromium keeps its renderer sandbox where it is supported. Playwright passed
  `--no-sandbox`.
- Clicks hold the mouse button for 45–110 ms and keystrokes hold the key for
  35–95 ms. Chromium released both in under 1 ms.
- Camoufox no longer prints a `LeakWarning` about `ff_version` on each launch.
  Wraith sets `ff_version` to the version of its pinned build.

### Added

- `tests/test_live_fingerprint.py`: offline checks on 127.0.0.1 for automation
  signs (fingerprint, worker and header User-Agent, WebGL, window sizes),
  human-like input events through `AgentBrowser`, and Chromium launch flags.
  The macOS CI job runs them.

## [0.9.0] - 2026-10-17

### Added

- Domain allowlist: `AgentBrowser(allowed_domains="a.com,*.b.com")`,
  `wraith mcp --allowed-domains`, and `WRAITH_ALLOWED_DOMAINS`. The browser
  refuses navigations, new tabs, requests, and WebSockets to other hosts. It
  closes a page that a server redirect moved off the list and raises
  `DomainNotAllowedError`. Snapshots leave out frames on other hosts.
- The MCP `fetch` and `detect_waap` tools check the allowlist before they send
  a request.
- The MCP server wraps page text in `<untrusted-page-content id="…">` markers
  with a random id. `WRAITH_CONTENT_BOUNDARIES=0` turns them off.
- `wraith.DomainPolicy` and `wraith.DomainNotAllowedError`.

### Changed

- The macOS CI job runs every live browser test that uses only local fixture
  pages: launch, frames, Press & Hold, Clawd, secret policy, the Chromium
  handoff, and the domain allowlist.

## [0.8.0] - 2026-10-17

### Added

- Snapshots index elements inside visible iframes, same-origin and
  cross-origin, including nested frames. Indices continue the main sequence,
  lines end with `(frame N)`, and `click()` / `type()` act on them. An overlay
  above an iframe hides its covered elements.
- Snapshots list frames under `Frames:` with their on-screen box. A frame
  that cannot be read (a closed shadow root, such as Cloudflare Turnstile)
  says so.
- `click_frame(frame, x, y)` (library and MCP) clicks a fraction of a frame's
  box, measured at click time, so a moving frame is still hit.
- `click_at(x, y)` (library and MCP) clicks a page point in CSS pixels after a
  human-like mouse path.
- `AgentBrowser.screenshot(scale=...)`. The MCP `screenshot` uses CSS scale.

### Security

- Secret fills (`fill_secret`, `fill_vault_item`) refuse frame fields.

## [0.7.1] - 2026-10-17

### Fixed

- `type()` works on fields that replace their node on input, such as the
  Wikipedia search box. Keys follow focus, the clear uses keys, and the check
  reads the focused control.
- `click()` and `type(..., enter=True)` wait for a navigation that they start,
  so the snapshot shows the new page. An action that starts no navigation
  waits at most 500 ms.
- `type()` refuses a target that does not take text before it clicks. A stale
  index no longer clicks a link or a button.
- `navigate()` raises `NavigationFailedError` when the URL cannot load (for
  example a DNS or connection error). It no longer reports the previous page
  as cleared.
- MCP tool errors reach the client with their reason on mcp 2.x. Before, the
  client got only "Error executing tool <name>".

## [0.7.0] - 2026-10-17

### Added

- `wraith.chromium_storage_state()` and `wraith.export_storage_state()` make a
  Camoufox session loadable in Chromium tools such as agent-browser.
  Non-secure `SameSite=None` cookies become `Lax`, the Chromium default.
- `AgentBrowser.save_storage_state(path, for_chromium=True)` and MCP
  `save_state(path, for_chromium=True)` write that format.
- `wraith borrow --save-state PATH --for-chromium` saves the borrowed session.
  In headed mode, Wraith saves after you press Enter.
- The README explains the agent-browser handoff and its limits.

## [0.6.5] - 2026-10-17

### Fixed

- Camoufox launches on macOS again. Version 0.6.4 failed with a missing
  `Contents/MacOS/properties.json` error, so `wraith selftest` failed too.
- Wraith now reads `properties.json` from the app bundle `Contents/Resources`
  folder. Camoufox 0.5.8 has the same fallback.

### Added

- CI launches the pinned Camoufox build on macOS.

## [0.6.4] - 2026-09-29

### Fixed

- Pinned Camoufox to Python package `0.5.6` and browser `152.0.4-beta.30`.
- Wraith no longer fetches a newer browser with an incompatible config schema.
- Missing pinned browsers now show the exact install command.

## [0.6.3] - 2026-09-29

### Fixed

- Optional detector probes no longer fail the self-test when they remain unknown.
- Required detector checks still fail closed when their measurement is unknown.

## [0.6.2] - 2026-09-29

### Fixed

- Self-test now reads the current detector table when the detector JSON field is empty.
- Added a live end-to-end detector regression test.

## [0.6.1] - 2026-09-29

### Fixed

- Self-test now fails when a detector result is unknown. An exit code of zero no longer means that missing checks passed.
- Managed Clawd runs now require a healthy, active Tailscale exit node identity. A matching public IP alone is not enough.

## [0.6.0] - 2026-09-29

### Added

- Opaque vault-item fills. A registered provider resolves an item inside Wraith, and the MCP tool returns only success.
- Coordinate-based interactive snapshots. Covered controls stay out of the action list, and clicks use visible points.
- Redacted browser action errors and a versioned `clawd-run` flow for Tailnet setup, home-exit checks, vault canaries, and the final browser command.
- Regression tests and documentation for the secure Clawd flow.

### Security

- Vault material is cleared after each fill. Managed command output stays inside Wraith.
- The release provides a provider interface. It does not include a direct 1Password or `opgate` adapter.

## [0.5.0] - 2026-09-29

### Added

- Automatic HUMAN/PerimeterX Press & Hold attempts through `clear_challenge()`, `AgentBrowser.navigate()`, and MCP `navigate`.
- Challenge detection across child frames and open shadow roots, with coordinate targeting for visible closed shadow hosts.
- At most two holds, each lasting up to eight seconds within the polling timeout, with mouse release after each attempt.
- The `press_hold=False` option disables automatic input while preserving challenge detection.
- MCP server instructions and navigation descriptions explain automatic challenge handling to agents.
- Offline regression tests and five opt-in Camoufox browser fixtures.

### Fixed

- Existing cookies and HTTP 200 challenge pages no longer cause false HUMAN clearance.
- Clearance waits through `settle` for delayed widgets and requires a new successful main-frame response after HUMAN detection.
- Child-frame responses no longer replace the tracked main-frame HTTP status.

### Verification limits

- Live iHerb loaded without a challenge during testing. Acceptance of its current Press & Hold challenge remains unverified.

## [0.4.2] - 2026-09-27

### Added

- Added an opt-in Camoufox secret-policy harness for local tests. It uses
  direct `page.goto`, checks the browser policy, and hashes field values as
  strings inside Camoufox.

## [0.4.1] - 2026-09-27

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

- **Secret provider file imports**: `wraith.secrets` now loads through a
  file-based provider shim without requiring a `sys.modules` registration.

### Changed

- **Site link previews**: the landing page now serves a branded Open Graph
  image and declares Open Graph and Twitter card metadata.

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

[Unreleased]: https://github.com/YogevKr/wraith/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/YogevKr/wraith/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/YogevKr/wraith/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/YogevKr/wraith/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/YogevKr/wraith/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/YogevKr/wraith/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/YogevKr/wraith/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/YogevKr/wraith/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/YogevKr/wraith/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/YogevKr/wraith/releases/tag/v0.1.0
