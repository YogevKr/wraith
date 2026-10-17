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


def html_pages(
    pages: dict[str, str], default: str
) -> type[http.server.BaseHTTPRequestHandler]:
    """A handler that serves ``pages[path]`` as HTML, else ``default``."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(pages.get(self.path, default).encode())

        def log_message(self, *args: object) -> None:
            pass

    return Handler
