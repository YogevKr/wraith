# wraithbrowser.dev — project site

The landing page for Wraith, served by a small Cloudflare Worker that returns a
single self-contained static HTML document (no external requests, no build).

- `worker.js` — the Worker; the page is inlined as an HTML string. `www.` folds
  to the apex with a 301.
- `wrangler.toml` — binds the apex + `www` as custom domains.

## Deploy

```bash
cd site
wrangler deploy      # provisions DNS + cert for the custom domains automatically
```

Live at <https://wraithbrowser.dev>. The dead-drop relay is a **separate**
Worker (see [`../deploy/`](../deploy/)) on `drop.wraithbrowser.dev`; the two
Workers share the zone but never overlap hostnames.

## Forking

Point it at your own domain: change `name` and the two `[[routes]]` patterns in
`wrangler.toml`, and update the URLs referenced in `worker.js` (the GitHub links
and the `drop.<domain>` relay host in the quick-start snippet).
