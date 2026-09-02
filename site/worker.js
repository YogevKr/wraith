/**
 * wraith-site — the landing page for Wraith, served at wraithbrowser.dev.
 * A single static HTML document, no external requests. www -> apex redirect.
 */

const HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wraith — the identity-borrowing stealth browser for agents</title>
<meta name="description" content="Wraith is a stealth, identity-borrowing, MCP-native browser for autonomous agents. Don't beat reputation defenses — borrow a warmed identity.">
<meta property="og:title" content="Wraith">
<meta property="og:description" content="The identity-borrowing stealth browser for autonomous agents.">
<meta property="og:type" content="website">
<style>
  :root{
    --bg:#0a0b0e; --panel:#111318; --line:#20242c; --fg:#e7e9ee; --muted:#9aa3b2;
    --accent:#5eead4; --accent-dim:#2dd4bf; --code:#0d0f13; --warn:#f0b775;
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{
    margin:0;background:radial-gradient(1200px 600px at 50% -10%,#12161d 0%,var(--bg) 60%);
    color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  .wrap{max-width:960px;margin:0 auto;padding:0 24px}
  code,kbd,pre{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace}

  header{padding:88px 0 40px;text-align:center}
  .ghost{font-size:56px;line-height:1;filter:drop-shadow(0 6px 24px rgba(94,234,212,.25))}
  h1{font-size:clamp(40px,8vw,76px);margin:14px 0 6px;letter-spacing:-.03em;font-weight:800}
  .tag{color:var(--muted);font-size:clamp(16px,3.4vw,21px);margin:0 auto;max-width:640px}
  .accent{color:var(--accent)}
  .cta{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-top:28px}
  .btn{display:inline-block;padding:11px 20px;border-radius:10px;border:1px solid var(--line);
    background:var(--panel);color:var(--fg);font-weight:600}
  .btn.primary{background:var(--accent);color:#04120f;border-color:transparent}
  .btn:hover{text-decoration:none;border-color:var(--accent-dim)}

  .lede{margin:8px 0 12px;color:var(--muted);font-size:18px;text-align:center}
  section{padding:40px 0;border-top:1px solid var(--line)}
  h2{font-size:14px;text-transform:uppercase;letter-spacing:.16em;color:var(--muted);margin:0 0 22px;font-weight:700}

  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 18px 16px}
  .card h3{margin:0 0 6px;font-size:17px}
  .card p{margin:0;color:var(--muted);font-size:14.5px}
  .card .ic{font-size:20px;margin-bottom:8px;display:block}

  pre{background:var(--code);border:1px solid var(--line);border-radius:12px;padding:16px 18px;overflow-x:auto;font-size:14px;color:#d6dbe4}
  pre .c{color:var(--muted)}
  pre .g{color:var(--accent)}
  .two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:640px){.two{grid-template-columns:1fr}}

  .note{color:var(--muted);font-size:13.5px;margin-top:10px}
  .badge{display:inline-block;padding:2px 9px;border:1px solid var(--line);border-radius:999px;color:var(--muted);font-size:12px}

  footer{padding:44px 0 64px;border-top:1px solid var(--line);color:var(--muted);font-size:13.5px;text-align:center}
  footer a{color:var(--muted);text-decoration:underline}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="ghost">&#x1F47B;</div>
    <h1>Wraith</h1>
    <p class="tag">The <span class="accent">identity-borrowing</span> stealth browser for autonomous agents. Don't beat reputation defenses — <span class="accent">borrow</span> a warmed identity.</p>
    <div class="cta">
      <a class="btn primary" href="https://github.com/YogevKr/wraith">GitHub &rarr;</a>
      <a class="btn" href="https://github.com/YogevKr/wraith/releases">Releases</a>
    </div>
    <p class="note"><span class="badge">MIT</span> &nbsp; open source &nbsp;&middot;&nbsp; Camoufox + Playwright &nbsp;&middot;&nbsp; MCP-native</p>
  </header>

  <section>
    <h2>Why borrowing, not solving</h2>
    <p class="lede">reCAPTCHA-v3 has no solver — it is a reputation score. A fresh automated profile scores like a bot no matter how good the stealth. So Wraith reads a warmed, already-authenticated session out of a real browser profile and navigates as that trusted user. The reputation comes along for free.</p>
  </section>

  <section>
    <h2>What it does</h2>
    <div class="grid">
      <div class="card"><span class="ic">&#x1F3AD;</span><h3>Stealth engine</h3><p>Camoufox (Firefox-engine) primary, patched-Chromium fallback. Sidesteps the Chrome-specific detection cluster entirely.</p></div>
      <div class="card"><span class="ic">&#x1F6E1;&#xFE0F;</span><h3>WAAP challenge clearing</h3><p>Passes JS interstitials and fingerprints 12+ vendors: Cloudflare, Akamai, DataDome, Kasada, Imperva, Reblaze/Link11, and more.</p></div>
      <div class="card"><span class="ic">&#x1F511;</span><h3>Identity borrowing</h3><p>Inject warmed cookies from a real Firefox / Zen / Chrome profile to clear reputation-based defenses like reCAPTCHA-v3.</p></div>
      <div class="card"><span class="ic">&#x1F510;</span><h3>Profile sync</h3><p>Move a domain-scoped login to a remote Wraith over an anonymous, end-to-end-encrypted dead-drop. No account, no inbound port; the agent never sees your password.</p></div>
      <div class="card"><span class="ic">&#x1F9E9;</span><h3>MCP-native</h3><p>Drive it from Claude or any agent over MCP tools: navigate, snapshot, click, type, borrow, receive_profile.</p></div>
      <div class="card"><span class="ic">&#x26A1;</span><h3>No-browser fast path</h3><p>Replay a captured session with a real-browser TLS + HTTP/2 fingerprint and zero browser launch. Residential proxy rotation built in.</p></div>
    </div>
  </section>

  <section>
    <h2>Quick start</h2>
    <div class="two">
      <div>
        <pre><span class="c"># install (from source)</span>
uv add <span class="g">"wraith @ git+https://github.com/YogevKr/wraith"</span>
<span class="c"># then fetch the stealth browser</span>
uv run camoufox fetch</pre>
      </div>
      <div>
        <pre><span class="c"># drive it from the CLI</span>
wraith borrow <span class="g">https://example.com</span> --host example.com
wraith detect <span class="g">https://example.com</span>
<span class="c"># or run the MCP server for your agent</span>
wraith mcp</pre>
      </div>
    </div>
    <p class="note">Sync a login to a remote Wraith over the encrypted dead-drop:</p>
    <pre>wraith profile sync   --relay <span class="g">https://drop.wraithbrowser.dev</span> --from chrome --domain example.com
wraith profile receive <span class="g">&lt;pairing-code&gt;</span> --open https://example.com/   <span class="c"># the remote side</span></pre>
  </section>

  <footer>
    Wraith &middot; MIT-licensed &middot; <a href="https://github.com/YogevKr/wraith">github.com/YogevKr/wraith</a><br>
    An open-source project. No warranty. Use only against systems you are authorized to test.
  </footer>

</div>
</body>
</html>`;

export default {
  async fetch(request) {
    const url = new URL(request.url);
    // Fold www -> apex for a single canonical host.
    if (url.hostname.startsWith("www.")) {
      url.hostname = url.hostname.slice(4);
      return Response.redirect(url.toString(), 301);
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405 });
    }
    return new Response(HTML, {
      headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "public, max-age=300",
      },
    });
  },
};
