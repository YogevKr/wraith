# Opaque Secret Fills

An agent can fill a password, OTP, or card field without seeing the value.
This page covers secret capabilities and vault fills. See
[SECURITY.md](../SECURITY.md#opaque-secret-capabilities) for the trust limits.

## Secret capabilities

Use `fill_secret()` when an agent must fill a secret field. Do not give the
agent the secret value. Give it an opaque capability from your secret broker.

The capability contains these fields:

| Field | Purpose |
| --- | --- |
| `provider` | Selects a registered provider. |
| `handle` | Identifies provider-owned secret material. Wraith redacts it from representations. |
| `allowed_origins` | Lists exact HTTP or HTTPS origins. Scheme, host, and non-default port must match. |
| `field_kind` | Limits the target field to `password`, `username`, `email`, `otp`, `card-number`, `card-expiry`, `card-cvc`, or `text`. |
| `expires_at` | Sets an optional ISO 8601 expiry with a time zone. |
| `max_uses` | Sets the browser-side use limit. The default is one. |
| `capability_id` | Carries an optional provider audit identifier. Wraith always counts uses by provider and handle. |

Secret targets must use main-frame `input` or `textarea` elements. Wraith does
not accept contenteditable targets because readable page text can expose them.
Username and OTP fields must declare `autocomplete="username"` or
`autocomplete="one-time-code"`.

Register the provider before the fill. A provider must return `SecretMaterial`.
It must authenticate and consume the handle. It must also enforce trusted
policy at the broker boundary.

```python
from wraith import SecretMaterial, agent_browser, register_secret_provider


class BrokerProvider:
    def resolve(self, capability, context):
        # broker.consume() must authenticate the opaque handle.
        # It must check context.origin and its own policy.
        value = broker.consume(capability.handle, context=context)
        return SecretMaterial(value)


register_secret_provider("broker", BrokerProvider())

with agent_browser(engine="camoufox", headless=True) as ab:
    ab.navigate("https://accounts.example.com/login")
    ab.fill_secret(7, {
        "provider": "broker",
        "handle": "opaque-capability-token",
        "allowed_origins": ["https://accounts.example.com"],
        "field_kind": "password",
        "expires_at": "2030-09-01T12:00:00Z",
        "max_uses": 1,
    })
```

You can also pass `secret_providers={"broker": BrokerProvider()}` to
`agent_browser()`. This keeps the provider on one browser instance.

A successful fill marks the session as secret-tainted. Wraith then blocks
`save_storage_state()` and `screenshot()` by default. Library callers can set
`allow_secret_tainted=True` to accept either risk.

For embedded MCP, register the provider in the MCP server process before
`app.run()`:

```python
from wraith import register_secret_provider
from wraith.mcp import app

register_secret_provider("broker", BrokerProvider())
app.run()
```

The MCP agent calls `fill_secret(index, capability)`. The tool accepts and
returns no secret value. The normal `wraith-mcp` command has no provider by
default. A provider registration in another process does not affect it.

Wraith has no Instinct Vault provider. Direct use needs an Instinct provider or
broker adapter that Wraith can reach. See [Security Policy](../SECURITY.md#opaque-secret-capabilities)
for trust limits and observed Instinct behavior.

## Vault fills

Vault adapters implement `authorize_item(item_id, origin)` and
`resolve_item(item_id, context)`, then register with `register_vault_provider()`. The MCP `fill_vault_item` tool accepts an item ID,
field kind, and provider name. It returns only `true` after the visible target
accepts the value. It returns no snapshot or secret value.
