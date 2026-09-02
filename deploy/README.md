# Wraith dead-drop relay — deploy & operate

The relay is a stateless [Cloudflare Worker](worker.js) that stores one
end-to-end-encrypted blob per random slot for ~10 minutes and hands it over
exactly once. It never sees the ephemeral secret, so it cannot read, forge, or
replay a transfer. See [`../SECURITY.md`](../SECURITY.md#profile-sync-dead-drop)
for the trust model.

## Deploy

```bash
cd deploy
npm i -g wrangler            # or: brew install cloudflare-wrangler
wrangler login              # OAuth; or set CLOUDFLARE_API_TOKEN with
                            # Workers Scripts:Edit + Workers KV Storage:Edit

# 1. Create the KV namespace and paste its id into wrangler.toml (DROPS binding).
wrangler kv namespace create DROPS

# 2. Deploy. Prints https://wraith-drop.<your-subdomain>.workers.dev
wrangler deploy
```

Verify it is live (uniform 404s, PUT→204, single-use GET):

```bash
URL=https://wraith-drop.<your-subdomain>.workers.dev
S=$(python3 -c 'import secrets;print(secrets.token_hex(16))')
curl -s -o /dev/null -w "%{http_code}\n" "$URL/s/$S"                 # 404 (empty)
curl -s -o /dev/null -w "%{http_code}\n" -X PUT --data x "$URL/s/$S" # 204
curl -s -o /dev/null -w "%{http_code}\n" "$URL/s/$S"                 # 200
curl -s -o /dev/null -w "%{http_code}\n" "$URL/s/$S"                 # 404 (spent)
```

## Configuration

| Knob | Where | Default | Meaning |
| --- | --- | --- | --- |
| Body cap | `worker.js` `MAX_BYTES` + `wraith.deaddrop.MAX_BLOB_BYTES` | 1 MiB | Max blob size. Keep the two in sync. |
| TTL | `worker.js` `TTL_SECONDS` + `deaddrop._DEFAULT_MAX_AGE` | 600 s | How long a drop lives / stays fresh. Keep in sync. |
| Rate limit | `wrangler.toml` `DROP_LIMITER` | 120 / 60 s per IP | Caps request rate per client IP. `period` must be 10 or 60. |
| Pad bucket | `deaddrop._BUCKET` | 16 KiB | Blob-size granularity (hides the jar size). |

## Operate

- **Cost.** Well within the Workers free tier for personal use (100k req/day,
  1k KV writes/day, 100k KV reads/day). Each transfer is one PUT + one GET + one
  delete. Monitor in the Cloudflare dashboard → Workers → wraith-drop → Metrics.
- **Rate-limit tuning.** Raise/lower `DROP_LIMITER.simple.limit` for more/less
  headroom; a burst past the limit returns 429 (the client retries with backoff).
- **Anonymity.** The relay sees ciphertext, a random slot, a padded size, and
  both IPs. Route the laptop through WARP/Tor to hide its IP from the relay
  operator. Use a throwaway Cloudflare account to hide ownership from Cloudflare.
- **Rotation.** Nothing to rotate — there is no long-lived secret. To retire the
  endpoint entirely: `wrangler delete` then delete the KV namespace
  (`wrangler kv namespace delete --namespace-id <id>`).

## Test the client against a local mirror

The Python client (`wraith.deaddrop`) is covered offline by
`tests/test_deaddrop_relay.py`, which runs a local HTTP server mirroring these
semantics (roundtrip, single-use, retry/backoff, size guard) — no Cloudflare
account needed for CI.
