"""Ephemeral shared-secret dead-drop — the anonymous, login-free transport.

The ``profile sync`` flow moves a cookie jar from a laptop to a remote Wraith
over the public internet WITHOUT any account, inbound port, or long-lived key.
Both ends connect *outbound* to a dumb relay (a Cloudflare Worker — see
``deploy/worker.js``) that only stores one ciphertext blob per random slot for a
few minutes. The relay never holds anything worth stealing.

All security lives at the two ends and rides on one ephemeral secret ``S``:

    S           16 random bytes, generated per transfer, valid ~10 minutes.
    slot        HKDF(S, "wraith-slot") -> 16 bytes -> the relay URL path.
                Unguessable (128-bit), so the relay sees only random ids.
    key         HKDF(S, "wraith-key")  -> 32 bytes -> ChaCha20-Poly1305 key.

The sender seals ``jar`` under ``key`` (AEAD, with ``slot`` as associated data
and a length-prefixed pad so the blob size hides the jar size), PUTs it to
``/s/<slot>``, and prints a pairing code carrying ``(relay_url, S)``. The
receiver parses the code, derives the same ``slot``/``key``, GETs the blob, and
opens it. The AEAD tag proves the sender held ``S`` — that is the whole
authentication, so there is no separate signature or password.

Move the pairing code out of band (ssh paste, 1Password, a QR on your phone).
Whoever holds it for those ~10 minutes can read that one transfer; after pickup
or expiry the slot is gone. ``S`` never touches the relay.

Threat notes (see SECURITY.md for the full table):
* The relay sees ciphertext, a random single-use slot, a padded size, and the
  two source IPs. It cannot read, forge, or replay a jar.
* A cookie jar is the session past 2FA. Keep syncs domain-scoped and short.
* Route the laptop through WARP/Tor if you also want to hide your IP from the
  relay operator.
"""

from __future__ import annotations

import base64
import os
import struct
import time
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CODE_PREFIX",
    "MAX_BLOB_BYTES",
    "DeadDropError",
    "DropAuthError",
    "DropExpired",
    "DropNotFound",
    "DropTooLarge",
    "RelayError",
    "SealedDrop",
    "burn",
    "derive",
    "format_code",
    "new_secret",
    "open_sealed",
    "parse_code",
    "pull",
    "push",
    "seal",
]

CODE_PREFIX = "wraith1"
_MAGIC = b"WRD1"
_SECRET_LEN = 16
_NONCE_LEN = 12
_BUCKET = 16384  # pad the plaintext up to a multiple of this to blur its size
_DEFAULT_MAX_AGE = 600  # seconds a drop stays fresh
# Must match the relay's body cap (deploy/worker.js MAX_BYTES). A sealed blob
# larger than this is rejected by the relay, so we fail early with a clear error.
MAX_BLOB_BYTES = 1024 * 1024
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 0.5  # seconds, doubled each retry


class DeadDropError(RuntimeError):
    """Base error for dead-drop operations."""


class DropAuthError(DeadDropError):
    """The blob did not open under this secret (wrong key or tampered)."""


class DropExpired(DeadDropError):
    """The drop's embedded timestamp is outside the freshness window."""


class DropNotFound(DeadDropError):
    """The relay has no blob at this slot (never sent, or already picked up)."""


class DropTooLarge(DeadDropError):
    """The sealed blob exceeds the relay's body cap; scope the sync narrower."""


class RelayError(DeadDropError):
    """The relay returned an unexpected status after retries."""


# --------------------------------------------------------------------------- #
# Secret + key derivation
# --------------------------------------------------------------------------- #

def new_secret() -> bytes:
    """Return a fresh 16-byte ephemeral transfer secret ``S``."""
    return os.urandom(_SECRET_LEN)


def _hkdf(secret: bytes, info: bytes, length: int) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(
        secret
    )


def derive(secret: bytes) -> tuple[str, bytes]:
    """Derive ``(slot_hex, aead_key)`` from an ephemeral secret.

    ``slot_hex`` is the relay URL path (32 hex chars). ``aead_key`` is the
    32-byte ChaCha20-Poly1305 key. Both come from ``S`` alone, so the sender and
    receiver land on the same slot and key with no round-trip.
    """
    if len(secret) != _SECRET_LEN:
        raise DeadDropError(f"secret must be {_SECRET_LEN} bytes, got {len(secret)}")
    slot = _hkdf(secret, b"wraith-slot", 16).hex()
    key = _hkdf(secret, b"wraith-key", 32)
    return slot, key


# --------------------------------------------------------------------------- #
# Pairing code (relay_url + secret), base64url, dot-separated
# --------------------------------------------------------------------------- #

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def format_code(relay_url: str, secret: bytes) -> str:
    """Format a one-shot pairing code ``wraith1.<relay>.<secret>``.

    Both fields are base64url (no ``=`` padding), joined by ``.`` — neither the
    prefix nor base64url contains a ``.``, so the code parses unambiguously and
    is safe to paste, ssh, or render as a QR.
    """
    if len(secret) != _SECRET_LEN:
        raise DeadDropError(f"secret must be {_SECRET_LEN} bytes, got {len(secret)}")
    url = relay_url.rstrip("/")
    return f"{CODE_PREFIX}.{_b64e(url.encode('utf-8'))}.{_b64e(secret)}"


def parse_code(code: str) -> tuple[str, bytes]:
    """Parse a pairing code back into ``(relay_url, secret)``."""
    parts = code.strip().split(".")
    if len(parts) != 3 or parts[0] != CODE_PREFIX:
        raise DeadDropError("not a wraith1 pairing code")
    try:
        relay_url = _b64d(parts[1]).decode("utf-8")
        secret = _b64d(parts[2])
    except (ValueError, UnicodeDecodeError) as exc:
        raise DeadDropError(f"corrupt pairing code: {exc}") from exc
    if len(secret) != _SECRET_LEN:
        raise DeadDropError("pairing code carries a wrong-length secret")
    return relay_url, secret


# --------------------------------------------------------------------------- #
# Seal / open
# --------------------------------------------------------------------------- #
# Wire layout inside the AEAD plaintext (before padding):
#   magic(4) | version(1)=1 | timestamp(8, big-endian uint) | jarlen(4) | jar
# then zero-padded up to a multiple of _BUCKET. The whole thing is sealed with
# ChaCha20-Poly1305; the on-wire blob is: nonce(12) | ciphertext+tag.

def _pad(body: bytes) -> bytes:
    target = ((len(body) // _BUCKET) + 1) * _BUCKET
    return body + b"\x00" * (target - len(body))


def seal(jar: bytes, secret: bytes, *, now: float | None = None) -> bytes:
    """Seal ``jar`` bytes under ``secret``; return the on-wire blob.

    The blob is ``nonce ‖ ChaCha20-Poly1305(...)`` with the slot as associated
    data and a length-prefixed, bucket-padded body so its size does not leak the
    jar size. Embeds a timestamp that :func:`open_sealed` checks for freshness.
    """
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    slot, key = derive(secret)
    ts = int(time.time() if now is None else now)
    header = _MAGIC + b"\x01" + struct.pack(">Q", ts) + struct.pack(">I", len(jar))
    padded = _pad(header + jar)
    nonce = os.urandom(_NONCE_LEN)
    ct = ChaCha20Poly1305(key).encrypt(nonce, padded, bytes.fromhex(slot))
    return nonce + ct


def open_sealed(
    blob: bytes,
    secret: bytes,
    *,
    now: float | None = None,
    max_age: int = _DEFAULT_MAX_AGE,
) -> bytes:
    """Open a sealed blob under ``secret``; return the original ``jar`` bytes.

    Raises :class:`DropAuthError` if the blob does not authenticate under this
    secret, or :class:`DropExpired` if its timestamp is older/newer than
    ``max_age`` seconds (replay / stale-drop guard).
    """
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    slot, key = derive(secret)
    if len(blob) < _NONCE_LEN + 16:
        raise DropAuthError("sealed blob is too short")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    try:
        padded = ChaCha20Poly1305(key).decrypt(nonce, ct, bytes.fromhex(slot))
    except Exception as exc:  # InvalidTag and friends
        raise DropAuthError("blob did not open under this secret") from exc

    if padded[:4] != _MAGIC:
        raise DropAuthError("bad magic after decrypt")
    (ts,) = struct.unpack(">Q", padded[5:13])
    (jarlen,) = struct.unpack(">I", padded[13:17])
    reference = int(time.time() if now is None else now)
    if abs(reference - ts) > max_age:
        raise DropExpired(f"drop is {abs(reference - ts)}s old (max {max_age}s)")
    jar = padded[17 : 17 + jarlen]
    if len(jar) != jarlen:
        raise DropAuthError("truncated jar after decrypt")
    return jar


@dataclass
class SealedDrop:
    """A sealed blob plus the slot it belongs at (for direct relay calls)."""

    slot: str
    blob: bytes


# --------------------------------------------------------------------------- #
# Relay client (httpx; PUT to send, GET to pick up)
# --------------------------------------------------------------------------- #

def _slot_url(relay_url: str, slot: str) -> str:
    return f"{relay_url.rstrip('/')}/s/{slot}"


def _request_with_retries(
    method: str,
    url: str,
    *,
    content: bytes | None = None,
    timeout: float,
    attempts: int = _RETRY_ATTEMPTS,
) -> Any:
    """Issue one relay request, retrying transient failures with backoff.

    Retries connection/timeout errors, HTTP 429, and 5xx (transient relay or
    edge hiccups). A 404 is NOT transient — it is a real "no drop" answer and
    returns immediately. Honors a ``Retry-After`` header on 429 when present.
    """
    import time as _time

    import httpx

    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = httpx.request(method, url, content=content, timeout=timeout)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            resp = None
        else:
            if resp.status_code < 500 and resp.status_code != 429:
                return resp
            last_exc = RelayError(f"relay {method} -> HTTP {resp.status_code}")
        if attempt == attempts - 1:
            break
        delay = _RETRY_BACKOFF * (2**attempt)
        if resp is not None and resp.status_code == 429:
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                delay = max(delay, float(retry_after))
        _time.sleep(delay)
    if last_exc is not None:
        raise RelayError(f"relay {method} failed after {attempts} attempts: {last_exc}")
    raise RelayError(f"relay {method} failed after {attempts} attempts")


def push(relay_url: str, jar: bytes, *, secret: bytes | None = None, timeout: float = 30.0) -> str:
    """Seal ``jar`` and PUT it to the relay; return the pairing code.

    Generates a fresh ephemeral ``secret`` unless one is supplied. The returned
    code carries ``(relay_url, secret)`` — hand it to the receiver out of band.
    Raises :class:`DropTooLarge` before any network call if the sealed blob
    exceeds the relay's body cap, and :class:`RelayError` on a persistent relay
    failure (retried with backoff).
    """
    secret = secret or new_secret()
    slot, _ = derive(secret)
    blob = seal(jar, secret)
    if len(blob) > MAX_BLOB_BYTES:
        raise DropTooLarge(
            f"sealed blob is {len(blob) // 1024} KiB, over the relay's "
            f"{MAX_BLOB_BYTES // 1024} KiB cap — scope the sync to fewer domains."
        )
    resp = _request_with_retries("PUT", _slot_url(relay_url, slot), content=blob, timeout=timeout)
    if resp.status_code not in (200, 201, 204):
        raise RelayError(f"relay PUT rejected the blob: HTTP {resp.status_code}")
    return format_code(relay_url, secret)


def pull(code: str, *, timeout: float = 30.0, max_age: int = _DEFAULT_MAX_AGE) -> bytes:
    """GET and open the drop named by a pairing ``code``; return ``jar`` bytes.

    Raises :class:`DropNotFound` when the relay has no blob at the slot (never
    sent, or already picked up), :class:`DropExpired` on a stale drop,
    :class:`DropAuthError` on a bad/tampered blob, or :class:`RelayError` on a
    persistent relay failure (retried with backoff).
    """
    relay_url, secret = parse_code(code)
    slot, _ = derive(secret)
    # GET consumes the drop (atomic read-and-delete), so it must NOT be retried:
    # a retry after a lost 200 would see 404 and lose the jar. A single attempt
    # means a transient failure leaves the blob intact for a manual re-run.
    resp = _request_with_retries("GET", _slot_url(relay_url, slot), timeout=timeout, attempts=1)
    if resp.status_code == 404:
        raise DropNotFound("no drop at this slot (never sent or already picked up)")
    if resp.status_code != 200:
        raise RelayError(f"relay GET failed: HTTP {resp.status_code}")
    return open_sealed(resp.content, secret, max_age=max_age)


def burn(code: str, *, timeout: float = 30.0) -> bool:
    """Delete (revoke) the drop named by a pairing ``code`` without reading it.

    Holding the secret means holding the slot, so the holder can burn the sealed
    blob at the relay — a cancel/revoke for a sender who mis-sent a drop or whose
    code leaked. This grants no power an attacker did not already have: anyone
    with the secret can already destroy a drop by pulling it (GET deletes on
    read). ``burn`` just makes the destroy explicit and skips decryption.

    Returns ``True`` when a blob was deleted. Raises :class:`DropNotFound` when
    the slot is already empty (picked up, expired, burned, or never sent), or
    :class:`RelayError` on a persistent relay failure (retried with backoff).
    """
    relay_url, secret = parse_code(code)
    slot, _ = derive(secret)
    resp = _request_with_retries("DELETE", _slot_url(relay_url, slot), timeout=timeout)
    if resp.status_code == 404:
        raise DropNotFound("no drop at this slot to burn (already gone or never sent)")
    if resp.status_code not in (200, 204):
        raise RelayError(f"relay DELETE failed: HTTP {resp.status_code}")
    return True
