/**
 * Wraith dead-drop relay — a dumb, anonymous mailbox on Cloudflare Workers.
 *
 * It stores ONE end-to-end-encrypted blob per random slot for a few minutes and
 * hands it over exactly once. It has no accounts, no login, no listing, and no
 * knowledge of what it carries: every payload is sealed by the sender under an
 * ephemeral secret the relay never sees (see wraith/deaddrop.py). The relay's
 * only jobs are to accept a PUT, return it once on GET, and forget it.
 *
 *   PUT /s/<slot>   body = sealed blob (<= 1 MiB). Stored with a short TTL.
 *   GET /s/<slot>   returns the blob once (200), then deletes it. 404 otherwise.
 *
 * Everything else — wrong method, bad slot, missing blob — returns an identical
 * 404 with no body, so the relay leaks nothing about what exists.
 *
 * Security lives at the two ends, not here. A compromised relay can delay or
 * drop a transfer; it cannot read, forge, or replay one. Slots are 128-bit and
 * single-use, so there is no useful endpoint to enumerate.
 *
 * Abuse control: a per-IP rate limit (Cloudflare's native Rate Limiting binding,
 * DROP_LIMITER in wrangler.toml) caps how fast one address can hit the relay, so
 * nobody can flood random-slot PUTs to burn KV storage or request quota. Over
 * the limit returns 429 (it reveals nothing about which slots exist).
 *
 * Deploy:  wrangler deploy   (needs a KV namespace bound as DROPS and the
 *          DROP_LIMITER rate-limit binding — see wrangler.toml). Use a throwaway
 *          Cloudflare account if you also want to hide ownership from Cloudflare.
 */

const SLOT_RE = /^[0-9a-f]{32}$/; // exactly the hex slot deaddrop.derive() mints
const MAX_BYTES = 1024 * 1024; // 1 MiB cap — a padded jar is far smaller
const TTL_SECONDS = 600; // a drop lives at most 10 minutes

const NOT_FOUND = () => new Response(null, { status: 404 });
const TOO_MANY = () => new Response(null, { status: 429 });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/s\/([^/]+)$/);
    if (!match) return NOT_FOUND();

    const slot = match[1];
    if (!SLOT_RE.test(slot)) return NOT_FOUND();

    // Per-IP rate limit before any storage work (skips gracefully if the
    // binding is absent, so the Worker still runs without it configured).
    if (env.DROP_LIMITER) {
      const ip = request.headers.get("cf-connecting-ip") || "unknown";
      const { success } = await env.DROP_LIMITER.limit({ key: ip });
      if (!success) return TOO_MANY();
    }

    if (request.method === "PUT") {
      const body = new Uint8Array(await request.arrayBuffer());
      if (body.byteLength === 0 || body.byteLength > MAX_BYTES) return NOT_FOUND();
      // Never overwrite a live slot: first writer wins for the TTL window.
      const existing = await env.DROPS.get(slot, { type: "arrayBuffer" });
      if (existing) return NOT_FOUND();
      await env.DROPS.put(slot, body, { expirationTtl: TTL_SECONDS });
      return new Response(null, { status: 204 });
    }

    if (request.method === "GET") {
      const blob = await env.DROPS.get(slot, { type: "arrayBuffer" });
      if (!blob) return NOT_FOUND();
      // Single pickup: delete before returning so a leaked slot is spent once.
      await env.DROPS.delete(slot);
      return new Response(blob, {
        status: 200,
        headers: { "content-type": "application/octet-stream" },
      });
    }

    return NOT_FOUND();
  },
};
