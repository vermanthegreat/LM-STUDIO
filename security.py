"""Loopback-oriented request guards for mutating routes."""

from __future__ import annotations

from fastapi import Request

from errors import ValidationError

_LOOPBACK_HOST_PREFIXES = ("127.0.0.1", "localhost", "[::1]")
_LOOPBACK_ORIGIN_PREFIXES = (
    "http://127.0.0.1",
    "http://localhost",
    "https://127.0.0.1",
    "https://localhost",
)


def _host_is_loopback(host: str) -> bool:
    host = host.split(":")[0].strip().lower()
    return any(host == prefix or host.startswith(prefix) for prefix in _LOOPBACK_HOST_PREFIXES)


def assert_safe_mutation_request(request: Request, *, port: int) -> None:
    """Reject cross-origin or non-loopback mutation attempts."""
    host = (request.headers.get("host") or "").strip()
    if host:
        if not _host_is_loopback(host):
            raise ValidationError(
                error_code="unsafe_host",
                message="Mutations are only allowed from the local application host.",
                status_code=403,
            )
        if ":" in host:
            _, host_port = host.rsplit(":", 1)
            if host_port.isdigit() and int(host_port) != port:
                raise ValidationError(
                    error_code="unsafe_host_port",
                    message="Mutations are only allowed on the configured local port.",
                    status_code=403,
                )

    origin = (request.headers.get("origin") or "").strip()
    if origin and not any(origin.startswith(prefix) for prefix in _LOOPBACK_ORIGIN_PREFIXES):
        raise ValidationError(
            error_code="unsafe_origin",
            message="Cross-origin mutations are not allowed.",
            status_code=403,
        )
