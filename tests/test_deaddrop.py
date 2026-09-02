"""Dead-drop crypto + pairing-code tests (no network).

These lock the end-to-end guarantees: a jar sealed under S opens only under the
same S, the blob authenticates the sender via the AEAD tag, the size is padded,
the timestamp gates freshness, and the pairing code round-trips.
"""

import pytest

from wraith import deaddrop as dd


def test_secret_length():
    assert len(dd.new_secret()) == 16


def test_derive_is_deterministic_and_split():
    s = dd.new_secret()
    slot1, key1 = dd.derive(s)
    slot2, key2 = dd.derive(s)
    assert slot1 == slot2 and key1 == key2
    assert len(bytes.fromhex(slot1)) == 16
    assert len(key1) == 32
    # slot and key are independent derivations.
    assert bytes.fromhex(slot1) != key1[:16]


def test_two_secrets_differ():
    slot_a, key_a = dd.derive(dd.new_secret())
    slot_b, key_b = dd.derive(dd.new_secret())
    assert slot_a != slot_b and key_a != key_b


def test_seal_open_roundtrip():
    s = dd.new_secret()
    jar = b'{"cookies": [{"name": "sid", "value": "abc"}]}'
    blob = dd.seal(jar, s)
    assert dd.open_sealed(blob, s) == jar


def test_open_fails_under_wrong_secret():
    jar = b"payload"
    blob = dd.seal(jar, dd.new_secret())
    with pytest.raises(dd.DropAuthError):
        dd.open_sealed(blob, dd.new_secret())


def test_tampered_blob_is_rejected():
    s = dd.new_secret()
    blob = bytearray(dd.seal(b"payload", s))
    blob[-1] ^= 0xFF  # flip a tag byte
    with pytest.raises(dd.DropAuthError):
        dd.open_sealed(bytes(blob), s)


def test_size_is_padded_to_hide_jar_size():
    s = dd.new_secret()
    small = dd.seal(b"x", s)
    medium = dd.seal(b"x" * 1000, s)
    # Both land in the same 16 KiB bucket -> identical on-wire length.
    assert len(small) == len(medium)
    # A jar past the bucket grows by exactly one bucket.
    big = dd.seal(b"x" * 20000, s)
    assert len(big) == len(small) + dd._BUCKET


def test_stale_drop_is_expired():
    s = dd.new_secret()
    blob = dd.seal(b"payload", s, now=1_000_000)
    with pytest.raises(dd.DropExpired):
        dd.open_sealed(blob, s, now=1_000_000 + 601)


def test_fresh_within_window():
    s = dd.new_secret()
    blob = dd.seal(b"payload", s, now=1_000_000)
    assert dd.open_sealed(blob, s, now=1_000_000 + 599) == b"payload"


def test_slot_is_associated_data():
    # Re-sealing the same jar under a different secret yields a blob that will
    # not open under the first secret even if an attacker swaps ciphertexts.
    s1, s2 = dd.new_secret(), dd.new_secret()
    blob2 = dd.seal(b"payload", s2)
    with pytest.raises(dd.DropAuthError):
        dd.open_sealed(blob2, s1)


def test_pairing_code_roundtrip():
    s = dd.new_secret()
    code = dd.format_code("https://drop.example.workers.dev/", s)
    assert code.startswith("wraith1.")
    assert "." in code and ":" not in code.split("wraith1.")[1]
    relay, secret = dd.parse_code(code)
    assert relay == "https://drop.example.workers.dev"  # trailing slash stripped
    assert secret == s


def test_parse_rejects_foreign_code():
    with pytest.raises(dd.DeadDropError):
        dd.parse_code("hunter2")
    with pytest.raises(dd.DeadDropError):
        dd.parse_code("wraith1.onlytwo")


def test_empty_jar_roundtrips():
    s = dd.new_secret()
    assert dd.open_sealed(dd.seal(b"", s), s) == b""


def test_large_jar_roundtrips():
    s = dd.new_secret()
    jar = b"k" * 250_000  # a fat multi-domain jar
    assert dd.open_sealed(dd.seal(jar, s), s) == jar
