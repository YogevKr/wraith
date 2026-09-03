/**
 * Wraith dead-drop relay — a dumb, anonymous mailbox on Cloudflare Workers.
 *
 * It stores ONE end-to-end-encrypted blob per random slot for a few minutes and
 * hands it over exactly once. It has no accounts, no login, no listing, and no
 * knowledge of what it carries: every payload is sealed by the sender under an
 * ephemeral secret the relay never sees (see wraith/deaddrop.py).
 *
 *   PUT    /s/<slot>   store a sealed blob (<= 1 MiB). Idempotent: re-PUTting
 *                      the identical bytes (a retry after a lost response)
 *                      succeeds instead of colliding.
 *   GET    /s/<slot>   return the blob once (200) and delete it in the SAME
 *                      atomic step, then 404 forever.
 *   DELETE /s/<slot>   burn the blob without reading it (204), so whoever holds
 *                      the secret can revoke a drop they mis-sent or whose code
 *                      leaked. Grants no new power — a GET already destroys it.
 *
 * Everything else — wrong method, bad slot, missing blob — returns an identical
 * 404 with no body, so the relay leaks nothing about what exists.
 *
 * Each slot maps to its own Durable Object, whose storage operations are
 * serialized per object. That makes read-and-delete ATOMIC: two racing pickups
 * of a leaked code cannot both read the blob (Cloudflare KV could not guarantee
 * this). The DO is SQLite-backed, so it runs on the Workers Free plan. A TTL
 * alarm wipes an unclaimed drop after ~10 minutes.
 *
 * Security lives at the two ends, not here. A compromised relay can delay or
 * drop a transfer; it cannot read, forge, or replay one. Slots are 128-bit and
 * single-use, so there is no useful endpoint to enumerate.
 *
 * Abuse control: a per-IP rate limit (Cloudflare's native Rate Limiting binding,
 * DROP_LIMITER in wrangler.toml) caps how fast one address can hit the relay, so
 * nobody can flood random-slot PUTs to burn storage or request quota. Over the
 * limit returns 429 (it reveals nothing about which slots exist).
 *
 * Deploy:  wrangler deploy   (needs the DROPBOX Durable Object + DROP_LIMITER
 *          rate-limit bindings — see wrangler.toml; no KV namespace required).
 */

const SLOT_RE = /^[0-9a-f]{32}$/; // exactly the hex slot deaddrop.derive() mints
const MAX_BYTES = 1024 * 1024; // 1 MiB cap — a padded jar is far smaller
const TTL_MS = 600_000; // a drop lives at most 10 minutes

const NOT_FOUND = () => new Response(null, { status: 404 });
const NO_CONTENT = () => new Response(null, { status: 204 });
const TOO_MANY = () => new Response(null, { status: 429 });

function bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

/** One slot's atomic mailbox. Storage ops are serialized within the object. */
export class DropBox {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    const method = request.method;

    if (method === "PUT") {
      const body = new Uint8Array(await request.arrayBuffer());
      if (body.byteLength === 0 || body.byteLength > MAX_BYTES) return NOT_FOUND();
      const existing = await this.state.storage.get("blob");
      if (existing) {
        // A retry after a lost 204 re-sends the identical bytes: accept it
        // (idempotent). A genuinely different body is a slot collision -> 404.
        return bytesEqual(existing, body) ? NO_CONTENT() : NOT_FOUND();
      }
      await this.state.storage.put("blob", body);
      await this.state.storage.setAlarm(Date.now() + TTL_MS);
      return NO_CONTENT();
    }

    if (method === "GET") {
      const blob = await this.state.storage.get("blob");
      if (!blob) return NOT_FOUND();
      // Atomic single pickup: delete before returning, in the same object turn.
      await this.state.storage.delete("blob");
      return new Response(blob, {
        status: 200,
        headers: { "content-type": "application/octet-stream" },
      });
    }

    if (method === "DELETE") {
      const blob = await this.state.storage.get("blob");
      await this.state.storage.delete("blob");
      return blob ? NO_CONTENT() : NOT_FOUND();
    }

    return NOT_FOUND();
  }

  /** TTL expiry: wipe an unclaimed drop. */
  async alarm() {
    await this.state.storage.deleteAll();
  }
}

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

    // Route to the slot's own Durable Object for an atomic mailbox.
    const id = env.DROPBOX.idFromName(slot);
    return env.DROPBOX.get(id).fetch(request);
  },
};
