"""A local HTTP server on 127.0.0.1 for the live browser tests."""

from __future__ import annotations

import contextlib
import http.server
import threading
from typing import Iterator


@contextlib.contextmanager
def serve(
    handler: type[http.server.BaseHTTPRequestHandler],
) -> Iterator[http.server.HTTPServer]:
    """Serve ``handler`` on a free port until the block ends."""
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
