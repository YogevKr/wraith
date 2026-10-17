"""Network log: the requests the browser made, for an agent to inspect.

A site's own JSON API is often the cleanest way to read its data. The log lets
an agent find that API (method, URL, status, content type) and read a
response body, then call it with the fast path.

The log keeps the last :data:`MAX_ENTRIES` requests of the browser context. It
records no request headers or request bodies. :class:`~wraith.agent.AgentBrowser`
refuses response bodies after a secret fill and drops query strings from the
URLs it returns, because both can carry the secret or a session token.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

__all__ = ["NetworkEntry", "NetworkLog", "MAX_ENTRIES", "describe"]

MAX_ENTRIES = 500
_TEXT_TYPES = ("text/", "json", "javascript", "xml", "x-www-form-urlencoded")


@dataclass
class NetworkEntry:
    """One request and, once it arrives, its response."""

    id: int
    method: str
    url: str
    resource_type: str
    status: Optional[int] = None
    content_type: str = ""
    failure: str = ""
    response: Any = field(default=None, repr=False, compare=False)

    def to_dict(self, *, strip_query: bool = False) -> dict:
        url = _without_query(self.url) if strip_query else self.url
        return {
            "id": self.id,
            "method": self.method,
            "url": url,
            "type": self.resource_type,
            "status": self.status,
            "content_type": self.content_type,
            "failure": self.failure,
        }


def describe(entry: dict) -> str:
    """One line for a :meth:`NetworkEntry.to_dict` entry: ``#id METHOD status type url``."""
    status = entry["status"] if entry["status"] is not None else "-"
    line = f"#{entry['id']} {entry['method']} {status} {entry['type']} {entry['url']}"
    if entry["content_type"]:
        line += f"  [{entry['content_type'].split(';')[0]}]"
    if entry["failure"]:
        line += f"  FAILED: {entry['failure']}"
    return line


class NetworkLog:
    """Records the requests of a Playwright browser context."""

    def __init__(self, limit: int = MAX_ENTRIES) -> None:
        self._entries: deque[NetworkEntry] = deque(maxlen=limit)
        # Requests whose response or failure has not arrived yet.
        self._open: OrderedDict[Any, NetworkEntry] = OrderedDict()
        self._limit = limit
        self._next_id = 1

    def install(self, context: Any) -> None:
        """Start recording ``context``'s requests."""
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfailed", self._on_failed)
        context.on("requestfinished", self._on_finished)

    def _on_request(self, request: Any) -> None:
        entry = NetworkEntry(
            id=self._next_id,
            method=request.method,
            url=request.url,
            resource_type=request.resource_type,
        )
        self._next_id += 1
        self._entries.append(entry)
        self._open[request] = entry
        while len(self._open) > 2 * self._limit:  # requests that never ended
            self._open.popitem(last=False)

    def _on_response(self, response: Any) -> None:
        entry = self._open.get(response.request)
        if entry is not None:
            entry.status = response.status
            entry.content_type = (response.headers or {}).get("content-type", "")
            entry.response = response

    def _on_failed(self, request: Any) -> None:
        entry = self._open.pop(request, None)
        if entry is not None:
            entry.failure = request.failure or "failed"

    def _on_finished(self, request: Any) -> None:
        self._open.pop(request, None)

    def entries(
        self, *, contains: str = "", resource_type: str = "", limit: int = 50
    ) -> list[NetworkEntry]:
        """The last ``limit`` entries whose URL contains ``contains``."""
        found = [
            entry
            for entry in self._entries
            if contains.lower() in entry.url.lower()
            and (not resource_type or entry.resource_type == resource_type)
        ]
        return found[-max(int(limit), 1) :]

    def entry(self, entry_id: int) -> NetworkEntry:
        """The entry with ``entry_id``; :class:`KeyError` when it left the log."""
        for entry in self._entries:
            if entry.id == int(entry_id):
                return entry
        raise KeyError(f"request #{entry_id} is not in the network log")

    def body(self, entry_id: int) -> str:
        """The response body of ``entry_id`` as text (a note for binary data)."""
        entry = self.entry(entry_id)
        if entry.response is None:
            reason = entry.failure or "no response arrived yet"
            raise LookupError(f"request #{entry.id} has no response body ({reason})")
        try:
            data = entry.response.body()
        except Exception as exc:
            raise LookupError(
                f"the browser no longer has the body of request #{entry.id} ({exc})"
            ) from exc
        kind = entry.content_type.lower()
        if not any(marker in kind for marker in _TEXT_TYPES):
            return f"(binary body: {len(data)} bytes, {entry.content_type or 'no content type'})"
        return data.decode("utf-8", errors="replace")


def _without_query(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
