"""Same-origin policy for authenticated, state-changing Admin HTTP requests."""
from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol

from .gateway_http_security import normalize_origin

Json = dict[str, Any]


class AdminSecurityHandler(Protocol):
    headers: Mapping[str, str]


JsonResponse = Callable[[AdminSecurityHandler, int, Json], None]
ErrorPayload = Callable[[str], Json]
AdminCheck = Callable[[AdminSecurityHandler], bool]
OriginCheck = Callable[[AdminSecurityHandler, Json], bool]


def request_origin(handler: AdminSecurityHandler) -> str | None:
    """Derive the browser-visible request origin from Host and proxy scheme.

    ``X-Forwarded-Host`` is intentionally ignored because the bundled reverse
    proxy preserves the public Host header and an untrusted direct client could
    otherwise spoof both Origin and X-Forwarded-Host. The proxy scheme remains
    necessary for TLS termination and is overwritten by the bundled Nginx.
    """
    host = str(handler.headers.get("Host") or "").split(",", 1)[0].strip()
    if not host:
        return None
    proto = str(handler.headers.get("X-Forwarded-Proto") or "http").split(",", 1)[0].strip().lower()
    return normalize_origin(f"{proto}://{host}")


def check_admin_origin(
    handler: AdminSecurityHandler,
    config: Json,
    *,
    json_response: JsonResponse,
    error_payload: ErrorPayload,
) -> bool:
    """Reject cross-origin browser writes while allowing non-browser CLI calls."""
    source = handler.headers.get("Origin") or handler.headers.get("Referer")
    if not source:
        return True
    source_origin = normalize_origin(source)
    if not source_origin:
        json_response(handler, 403, error_payload("cross-origin admin request rejected"))
        return False

    allowed = {request_origin(handler)}
    raw_gateway_config = config.get("gateway")
    gateway_config = raw_gateway_config if isinstance(raw_gateway_config, dict) else {}
    allowed.add(normalize_origin(str(gateway_config.get("public_base_url") or "")))
    if source_origin in {origin for origin in allowed if origin}:
        return True

    json_response(handler, 403, error_payload("cross-origin admin request rejected"))
    return False


def check_admin_write(
    handler: AdminSecurityHandler,
    *,
    check_admin: AdminCheck,
    check_origin: OriginCheck,
) -> bool:
    """Authenticate before loading config and applying browser-origin policy."""
    if not check_admin(handler):
        return False
    from .gateway_config import load_config

    return check_origin(handler, load_config())


_url_origin = normalize_origin
_request_origin = request_origin


__all__ = [
    "AdminCheck",
    "AdminSecurityHandler",
    "ErrorPayload",
    "JsonResponse",
    "OriginCheck",
    "_request_origin",
    "_url_origin",
    "check_admin_origin",
    "check_admin_write",
    "request_origin",
]
