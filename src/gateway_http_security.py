#!/usr/bin/env python3
"""HTTP exposure and browser-origin security helpers."""
from __future__ import annotations

import ipaddress
import os
import urllib.parse
from typing import Any, Mapping

Json = dict[str, Any]

_EXPOSURE_MODES = {"auto", "private", "external"}


def normalize_origin(value: object) -> str | None:
    """Return a canonical HTTP(S) origin without paths, queries, or fragments."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urllib.parse.urlparse(text)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if scheme not in {"http", "https"} or not host or "*" in host or not parsed.netloc:
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    if port and port != default_port:
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def is_loopback_host(host: object) -> bool:
    """Return whether a listener host is restricted to the local machine."""
    text = str(host or "").strip().lower().strip("[]")
    if text in {"localhost", "localhost.localdomain"} or text.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def public_exposure_mode(host: object, configured: object | None = None) -> str:
    """Resolve the effective listener exposure contract."""
    raw = str(
        configured
        if configured is not None
        else os.environ.get("GATEWAY_PUBLIC_EXPOSURE", "auto")
    ).strip().lower() or "auto"
    if raw not in _EXPOSURE_MODES:
        from .gateway_errors import ConfigError

        raise ConfigError(
            "invalid GATEWAY_PUBLIC_EXPOSURE",
            detail={"value": raw, "allowed": sorted(_EXPOSURE_MODES)},
        )
    if raw == "auto":
        return "private" if is_loopback_host(host) else "external"
    return raw


def validate_bind_security(
    host: object,
    config: Mapping[str, Any],
    *,
    exposure: object | None = None,
) -> str:
    """Fail closed when an externally exposed listener lacks strong credentials."""
    mode = public_exposure_mode(host, exposure)
    if mode != "external":
        return mode

    from .gateway_config import _verify_password
    from .gateway_errors import ConfigError

    admin = config.get("admin") if isinstance(config.get("admin"), dict) else {}
    password_hash = str(admin.get("password_hash") or "")
    if not password_hash:
        raise ConfigError("external Gateway listener requires an Admin password")
    if bool(admin.get("must_change_password")) or _verify_password("admin", password_hash):
        raise ConfigError(
            "external Gateway listener refuses the default Admin password",
            detail="set GATEWAY_ADMIN_PASSWORD to a non-default value and restart",
        )

    downstream_keys = config.get("downstream_keys")
    enabled_keys = [
        item
        for item in (downstream_keys if isinstance(downstream_keys, list) else [])
        if isinstance(item, dict) and item.get("enabled", True) and str(item.get("key_hash") or "")
    ]
    if not enabled_keys:
        raise ConfigError(
            "external Gateway listener requires at least one enabled downstream API key",
            detail="set GATEWAY_DOWNSTREAM_KEY or configure downstream_keys before external exposure",
        )
    return mode


def _gateway_section(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if config is None:
        try:
            from .gateway_config import _gateway_config

            return _gateway_config()
        except Exception:
            return {}
    gateway = config.get("gateway")
    return gateway if isinstance(gateway, dict) else config


def cors_allowed_origins(config: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    gateway = _gateway_section(config)
    raw = gateway.get("cors_allowed_origins") or []
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = []
    origins: list[str] = []
    for value in values:
        origin = normalize_origin(value)
        if origin and origin not in origins:
            origins.append(origin)
    return tuple(origins)


def cors_request_origin(handler: Any) -> str | None:
    return normalize_origin(getattr(handler, "headers", {}).get("Origin"))


def cors_origin_allowed(handler: Any, config: Mapping[str, Any] | None = None) -> bool:
    gateway = _gateway_section(config)
    raw_origin = getattr(handler, "headers", {}).get("Origin")
    if raw_origin is None or not str(raw_origin).strip():
        return True
    origin = cors_request_origin(handler)
    if not origin:
        return False
    if not bool(gateway.get("cors_enabled", False)):
        return False
    return origin in set(cors_allowed_origins(gateway))


def send_cors_headers(
    handler: Any,
    config: Mapping[str, Any] | None = None,
    *,
    preflight: bool = False,
) -> bool:
    """Emit CORS headers for an exact allowed Origin and return the decision."""
    origin = cors_request_origin(handler)
    raw_origin = getattr(handler, "headers", {}).get("Origin")
    if raw_origin is None or not str(raw_origin).strip():
        return True
    if not origin:
        handler.send_header("Vary", "Origin")
        return False
    allowed = cors_origin_allowed(handler, config)
    handler.send_header("Vary", "Origin")
    if not allowed:
        return False
    handler.send_header("Access-Control-Allow-Origin", origin)
    if preflight:
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key")
        handler.send_header("Access-Control-Max-Age", "600")
    return True


__all__ = [
    "cors_allowed_origins",
    "cors_origin_allowed",
    "cors_request_origin",
    "is_loopback_host",
    "normalize_origin",
    "public_exposure_mode",
    "send_cors_headers",
    "validate_bind_security",
]
