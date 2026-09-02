# Security Policy

## Supported Versions

Wraith is in active early development. Security fixes are applied to the latest
released version and to `main`.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Instead, report them privately through GitHub's
[private vulnerability reporting](https://github.com/YogevKr/wraith/security/advisories/new)
("Report a vulnerability" under the repository's **Security** tab). This keeps
the report confidential until a fix is available.

When reporting, please include as much of the following as you can:

- A description of the issue and its potential impact.
- Steps to reproduce, or a proof of concept.
- The affected version / commit.
- Any suggested remediation.

**Do not include real secrets in your report** — no live cookies, harvested
sessions, proxy credentials, or tokens. Redact them or use synthetic values.

## Response

We aim to acknowledge a report within a few days and to keep you informed as we
investigate and prepare a fix. Once a fix is released, we are happy to credit
reporters who wish to be named.

## Scope and Responsible Use

Wraith is a **dual-use** security/automation tool (a stealth, identity-borrowing
agent browser). Reports about how the project itself can be misused are best
directed at its design and documentation via a normal issue; this policy is for
**vulnerabilities in Wraith's own code** (e.g. credential handling, injection,
unsafe deserialization, accidental secret leakage).

Please read the **Responsible Use & Legal** section of the [README](README.md)
before using Wraith. Use it only for legitimate purposes: accessing your own
accounts and data, authorized security testing, research, and personal
automation.

## Opaque Secret Capabilities

Wraith can fill a browser field from an opaque secret capability. The agent
does not send a plain secret to `fill_secret()`.

The flow has four parts:

1. A secret broker creates an opaque capability.
2. The agent sends that capability to Wraith.
3. Wraith checks the browser target and calls a registered provider.
4. The provider resolves the handle, and Wraith fills the target field.

Wraith checks the exact current origin. It also checks the field kind, expiry,
and browser-side use count. Supported field kinds map to trusted DOM field
semantics. Secret fills reject contenteditable targets.

The capability fields are not proof by themselves. The provider must
authenticate and consume the handle. The broker must enforce all policy that a
modified client must not bypass.

Wraith clears the mutable `SecretMaterial` buffer after each fill attempt.
Python and the browser can create other memory copies. Buffer clearing does not
prove complete process-memory removal.

A successful fill marks the browser session as secret-tainted. Wraith blocks
storage-state export and screenshots after that point. A library caller can
allow either operation with `allow_secret_tainted=True`.

The snapshot layer omits `value` attributes from input, textarea, and
contenteditable elements. This limits common agent-output leaks. It does not
sanitize all page text or browser output.

### Trust Limits

The destination page receives the plain secret. Its scripts can read or send
that value. Wraith cannot protect a secret from the authorized destination.

The provider and browser process are trusted. Code in either process can access
the secret during a fill. Opaque capabilities protect the agent tool flow, not
the trusted runtime.

The page can copy a secret into normal text, logs, requests, storage, or another
field. Snapshot redaction cannot stop such page behavior.

An allowed screenshot can show non-password fields. Console output can contain
values that the page logs. Do not expose broad script execution or console
collection after a secret fill.

Use short expiry times and one use where possible. Bind each capability to the
smallest origin and field kind. Keep provider credentials outside the agent
context.

### Instinct Integration Findings

Testing on 2026-09-01 showed that the Instinct cloud browser runs outside the
sandbox. The sandbox controls it through harness tools.

The tested `vault_fill` result returned metadata only. The tested `read-page`,
`find`, and screenshot paths masked a password value.

The tested `execute-js` path read `input.value`. It returned data to the
sandbox. Therefore, `vault_fill` plus `execute-js` can expose plain secret text.

The tested console path leaked a value only when page code logged that value.
This result does not make console collection safe after a secret fill.

Wraith cannot directly use Instinct Vault today. Instinct must supply a provider
or broker adapter that Wraith can reach. That adapter should return an opaque
handle to the agent and resolve it only inside the trusted fill path.

## Profile Sync Dead-Drop

`wraith profile sync` moves a cookie jar from a laptop to a remote Wraith over a
relay. It uses no account and no long-lived key. One ephemeral secret protects
each transfer.

### What the transfer protects

The sender seals the jar with `ChaCha20-Poly1305`. The key comes from the
ephemeral secret. The relay slot comes from the same secret and is 128 bits, so
the relay sees only random single-use ids. The AEAD tag proves the sender held
the secret, so the transfer needs no separate signature. The blob is padded, so
its size does not reveal the jar size. The blob carries a timestamp, so a stale
or replayed drop is rejected.

### What the relay learns

The relay stores one ciphertext per slot for about ten minutes and returns it
once. It sees ciphertext, a random slot, a padded size, and the two source IP
addresses. It cannot read, forge, or replay a jar. A compromised relay can only
delay or drop a transfer.

The relay still sees both IP addresses and the transfer time. Route the laptop
through WARP or Tor to hide the laptop IP from the relay operator.

### What the secret controls

Whoever holds the pairing code for those ten minutes can read that one transfer.
Move the code out of band, such as over ssh, a password manager, or a QR code on
a phone. The code never travels through the relay.

The secret is per transfer and never stored. A leak burns one transfer, not the
account. Re-run the sync to make a new secret.

### The jar is the session past the second factor

A session cookie is the account after the password and the second factor. The
destination site cannot tell the agent from the human. So keep every sync scoped
to one domain with `--domain`. Use `--all` only when you accept uploading every
session. Re-sync only when the site logs the profile out.

### Cross-site egress

The remote uses its own exit IP, not the relay. A large geographic jump between
the laptop and the remote can log some sessions out and lower a reCAPTCHA-v3
reputation score. Pin a sticky residential exit near the account's home region
with the proxy pool when this matters.
