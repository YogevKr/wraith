"""Dead-drop relay CLIENT tests — the httpx push/pull path against a local
Worker-mirror. These cover the network code the pure-crypto tests don't:
roundtrip over HTTP, single-use, not-found, retry/backoff, and the size guard.
"""

import http.server
import threading

import pytest

from wraith import deaddrop as dd


class _Mirror(http.server.BaseHTTPRequestHandler):
    """Mirror of deploy/worker.js semantics, plus a programmable flaky mode.

    Class attributes drive behavior across the daemon thread:
      store        slot -> blob
      fail_puts    number of leading PUTs to answer 503 (then succeed)
      fail_gets    number of leading GETs to answer 503 (then serve)
      put_calls / get_calls   observed counts
    """

    store: dict = {}
    fail_puts = 0
    fail_gets = 0
    put_calls = 0
    get_calls = 0

    def log_message(self, *a):
        pass

    def _slot(self):
        return self.path.rsplit("/s/", 1)[-1] if "/s/" in self.path else None

    def do_PUT(self):
        type(self).put_calls += 1
        n = int(self.headers.get("content-length", 0))
        body = self.rfile.read(n)
        if type(self).fail_puts > 0:
            type(self).fail_puts -= 1
            self.send_response(503)
            self.end_headers()
            return
        s = self._slot()
        existing = type(self).store.get(s)
        if existing is not None:
            # Idempotent retry: identical bytes -> 204; different -> collision 404.
            self.send_response(204 if existing == body else 404)
            self.end_headers()
            return
        type(self).store[s] = body
        self.send_response(204)
        self.end_headers()

    def do_DELETE(self):
        s = self._slot()
        existed = type(self).store.pop(s, None)
        self.send_response(204 if existed is not None else 404)
        self.end_headers()

    def do_GET(self):
        type(self).get_calls += 1
        if type(self).fail_gets > 0:
            type(self).fail_gets -= 1
            self.send_response(503)
            self.end_headers()
            return
        s = self._slot()
        b = type(self).store.pop(s, None)
        if b is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


@pytest.fixture
def relay(monkeypatch):
    # Speed up retries so the backoff doesn't slow the suite.
    monkeypatch.setattr(dd, "_RETRY_BACKOFF", 0.001)
    _Mirror.store = {}
    _Mirror.fail_puts = 0
    _Mirror.fail_gets = 0
    _Mirror.put_calls = 0
    _Mirror.get_calls = 0
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Mirror)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def test_push_pull_roundtrip_over_http(relay):
    jar = b'{"cookies":[{"name":"sid","value":"abc"}]}'
    code = dd.push(relay, jar)
    assert code.startswith("wraith1.")
    assert dd.pull(code) == jar


def test_pull_is_single_use(relay):
    code = dd.push(relay, b"payload")
    assert dd.pull(code) == b"payload"
    with pytest.raises(dd.DropNotFound):
        dd.pull(code)


def test_pull_missing_slot_is_not_found(relay):
    # A valid code whose blob was never pushed.
    code = dd.format_code(relay, dd.new_secret())
    with pytest.raises(dd.DropNotFound):
        dd.pull(code)


def test_push_retries_past_transient_503(relay):
    _Mirror.fail_puts = 2  # first two PUTs 503, third succeeds
    code = dd.push(relay, b"payload")
    assert _Mirror.put_calls == 3
    assert dd.pull(code) == b"payload"


def test_pull_does_not_retry_and_preserves_blob_on_transient_failure(relay):
    # GET consumes, so it must NOT be retried: one 503 -> RelayError, exactly one
    # GET call, and the blob survives for a manual re-run.
    code = dd.push(relay, b"payload")
    _Mirror.fail_gets = 1
    with pytest.raises(dd.RelayError):
        dd.pull(code)
    assert _Mirror.get_calls == 1
    assert dd.pull(code) == b"payload"  # blob still there, second attempt serves it


def test_burn_deletes_the_drop(relay):
    code = dd.push(relay, b"payload")
    assert dd.burn(code) is True
    with pytest.raises(dd.DropNotFound):
        dd.pull(code)


def test_burn_missing_slot_is_not_found(relay):
    code = dd.format_code(relay, dd.new_secret())
    with pytest.raises(dd.DropNotFound):
        dd.burn(code)


def test_burn_after_pickup_is_not_found(relay):
    code = dd.push(relay, b"payload")
    assert dd.pull(code) == b"payload"
    with pytest.raises(dd.DropNotFound):
        dd.burn(code)


def test_put_retry_with_identical_bytes_is_idempotent(relay):
    # Simulate a lost 204: the same blob is re-PUT; the relay accepts it instead
    # of a first-writer-wins collision, so push still returns a usable code.
    secret = dd.new_secret()
    slot, _ = dd.derive(secret)
    blob = dd.seal(b"payload", secret)
    import httpx

    assert httpx.put(f"{relay}/s/{slot}", content=blob).status_code == 204
    assert httpx.put(f"{relay}/s/{slot}", content=blob).status_code == 204  # idempotent
    assert dd.pull(dd.format_code(relay, secret)) == b"payload"


def test_push_raises_relay_error_after_persistent_5xx(relay):
    _Mirror.fail_puts = 99  # never succeeds within the attempt budget
    with pytest.raises(dd.RelayError):
        dd.push(relay, b"payload")


def test_size_guard_rejects_oversize_before_network():
    # No relay needed: a jar past the cap must fail early with DropTooLarge.
    huge = b"x" * (dd.MAX_BLOB_BYTES + 1)
    with pytest.raises(dd.DropTooLarge):
        dd.push("http://127.0.0.1:1", huge)  # would refuse-connect if it tried
