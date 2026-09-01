"""Opaque secret capabilities for browser field fills.

The agent receives a capability, not a secret value. A registered provider
resolves the opaque handle only after Wraith checks the page origin and field.
"""

from __future__ import annotations

import hashlib
import ipaddress
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit

__all__ = [
    "SecretCapability",
    "SecretCapabilityError",
    "SecretError",
    "SecretMaterial",
    "SecretPolicyError",
    "SecretProvider",
    "SecretProviderError",
    "SecretRequestContext",
    "get_secret_provider",
    "register_secret_provider",
    "unregister_secret_provider",
]

SECRET_FIELD_KINDS = frozenset(
    {
        "password",
        "username",
        "email",
        "otp",
        "card-number",
        "card-expiry",
        "card-cvc",
        "text",
    }
)


class SecretError(RuntimeError):
    """Base error for secret capability operations."""


class SecretCapabilityError(SecretError):
    """The capability is invalid or exhausted."""


class SecretPolicyError(SecretError):
    """The browser target does not match the capability policy."""


class SecretProviderError(SecretError):
    """The named provider is missing or failed."""


def _parse_expiry(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise SecretCapabilityError("expires_at must use ISO 8601") from exc
    else:
        raise SecretCapabilityError("expires_at must be a datetime or string")
    if parsed.tzinfo is None:
        raise SecretCapabilityError("expires_at must include a time zone")
    return parsed.astimezone(timezone.utc)


def canonical_origin(url: str) -> str:
    """Return a strict HTTP origin without a path or query."""
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        port = parts.port
    except (TypeError, ValueError) as exc:
        raise SecretPolicyError("The page has an invalid origin") from exc
    if scheme not in {"http", "https"} or not host:
        raise SecretPolicyError("Secret fills require an HTTP or HTTPS origin")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise SecretPolicyError("The page has an invalid host name") from exc
    else:
        host = address.compressed
    default_port = 80 if scheme == "http" else 443
    formatted_host = f"[{host}]" if ":" in host else host
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{formatted_host}{suffix}"


@dataclass(frozen=True, repr=False)
class SecretCapability:
    """A provider handle with browser-side use limits.

    The provider must authenticate the opaque handle. It must also enforce any
    policy that must resist a modified client request.
    """

    provider: str
    handle: str = field(repr=False)
    allowed_origins: tuple[str, ...]
    field_kind: str
    expires_at: datetime | None = None
    max_uses: int = 1
    capability_id: str = ""

    def __post_init__(self) -> None:
        provider = self.provider.strip()
        handle = self.handle.strip()
        field_kind = self.field_kind.strip().lower().replace("_", "-")
        if not provider:
            raise SecretCapabilityError("provider is required")
        if not handle:
            raise SecretCapabilityError("handle is required")
        if not self.allowed_origins:
            raise SecretCapabilityError("allowed_origins must not be empty")
        if field_kind not in SECRET_FIELD_KINDS:
            raise SecretCapabilityError(f"Unsupported field_kind {field_kind!r}")
        if (
            isinstance(self.max_uses, bool)
            or not isinstance(self.max_uses, int)
            or self.max_uses < 1
        ):
            raise SecretCapabilityError("max_uses must be a positive integer")

        origins = tuple(canonical_origin(item) for item in self.allowed_origins)
        expiry = _parse_expiry(self.expires_at)
        identifier = self.capability_id.strip()
        if not identifier:
            source = f"{provider}\0{handle}".encode()
            identifier = hashlib.sha256(source).hexdigest()

        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "handle", handle)
        object.__setattr__(self, "allowed_origins", origins)
        object.__setattr__(self, "field_kind", field_kind)
        object.__setattr__(self, "expires_at", expiry)
        object.__setattr__(self, "max_uses", self.max_uses)
        object.__setattr__(self, "capability_id", identifier)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SecretCapability":
        """Parse an MCP-safe capability mapping."""
        if not isinstance(value, Mapping):
            raise SecretCapabilityError("capability must be an object")
        origins = value.get("allowed_origins", ())
        if isinstance(origins, str):
            origins = (origins,)
        elif isinstance(origins, (list, tuple)):
            origins = tuple(str(item) for item in origins)
        else:
            raise SecretCapabilityError("allowed_origins must be a list")
        return cls(
            provider=str(value.get("provider", "")),
            handle=str(value.get("handle", "")),
            allowed_origins=origins,
            field_kind=str(value.get("field_kind", "")),
            expires_at=_parse_expiry(value.get("expires_at")),
            max_uses=value.get("max_uses", 1),
            capability_id=str(value.get("capability_id", "")),
        )

    def __repr__(self) -> str:
        return (
            "SecretCapability("
            f"provider={self.provider!r}, handle=<redacted>, "
            f"allowed_origins={self.allowed_origins!r}, "
            f"field_kind={self.field_kind!r}, max_uses={self.max_uses!r})"
        )

    @property
    def usage_key(self) -> str:
        """Return a stable local use key that callers cannot rename."""
        source = f"{self.provider}\0{self.handle}".encode()
        return hashlib.sha256(source).hexdigest()


@dataclass(frozen=True)
class SecretRequestContext:
    """Trusted browser facts passed to the provider."""

    origin: str
    frame_origin: str
    field_kind: str
    field_tag: str
    field_type: str
    autocomplete: str
    index: int


class SecretMaterial:
    """A short-lived, mutable secret buffer with a redacted representation."""

    def __init__(self, value: str | bytes | bytearray) -> None:
        if isinstance(value, str):
            raw = value.encode("utf-8")
        elif isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
        else:
            raise TypeError("SecretMaterial requires text or bytes")
        self._buffer = bytearray(raw)
        self._cleared = False

    def reveal(self) -> str:
        """Decode the buffer for the browser fill call."""
        if self._cleared:
            raise SecretProviderError("Secret material is already clear")
        return self._buffer.decode("utf-8")

    def clear(self) -> None:
        """Overwrite the mutable buffer."""
        for index in range(len(self._buffer)):
            self._buffer[index] = 0
        self._buffer.clear()
        self._cleared = True

    @property
    def cleared(self) -> bool:
        return self._cleared

    def __enter__(self) -> "SecretMaterial":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.clear()

    def __repr__(self) -> str:
        return f"SecretMaterial(<redacted>, cleared={self._cleared})"


@runtime_checkable
class SecretProvider(Protocol):
    """Resolve an authenticated handle into short-lived secret material."""

    def resolve(
        self,
        capability: SecretCapability,
        context: SecretRequestContext,
    ) -> SecretMaterial:
        """Validate and consume the handle, then return secret material."""


_PROVIDERS: dict[str, SecretProvider] = {}
_PROVIDERS_LOCK = threading.RLock()


def register_secret_provider(
    name: str,
    provider: SecretProvider,
    *,
    replace: bool = False,
) -> None:
    """Register a provider for library and embedded MCP use."""
    key = name.strip()
    if not key:
        raise ValueError("Provider name is required")
    if not callable(getattr(provider, "resolve", None)):
        raise TypeError("Secret provider must define resolve")
    with _PROVIDERS_LOCK:
        if key in _PROVIDERS and not replace:
            raise ValueError(f"Secret provider {key!r} is already registered")
        _PROVIDERS[key] = provider


def unregister_secret_provider(name: str) -> None:
    """Remove a registered provider."""
    with _PROVIDERS_LOCK:
        _PROVIDERS.pop(name, None)


def get_secret_provider(name: str) -> SecretProvider:
    """Return a provider without exposing its configuration."""
    with _PROVIDERS_LOCK:
        provider = _PROVIDERS.get(name)
    if provider is None:
        raise SecretProviderError(f"Secret provider {name!r} is not registered")
    return provider
