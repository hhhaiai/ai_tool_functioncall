"""Typed Admin and downstream-client authentication for Gateway HTTP routes."""
from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol

from .gateway_errors import DownstreamAuthError
from .gateway_http_io import constant_time_equal, parse_basic_auth
from .gateway_http_security import send_cors_headers

Json = dict[str, Any]


class HTTPAuthHandler(Protocol):
    path: str
    headers: Mapping[str, str]

    def send_response(self, code: int, message: str | None = None) -> None: ...

    def send_header(self, keyword: str, value: str) -> None: ...

    def end_headers(self) -> None: ...


AuthErrorHandler = Callable[[HTTPAuthHandler, str, Exception], None]


def check_admin(
    handler: HTTPAuthHandler,
    *,
    handle_error: AuthErrorHandler,
) -> bool:
    """Authenticate one Admin request and opportunistically upgrade old hashes."""
    from .gateway_config import (
        _hash_password,
        _password_hash_needs_upgrade,
        _verify_password,
        load_config_with_revision,
        save_config,
    )

    try:
        cfg, revision = load_config_with_revision()
    except Exception as exc:
        handle_error(handler, handler.path.split("?", 1)[0], exc)
        return False

    raw_admin = cfg.get("admin")
    admin = raw_admin if isinstance(raw_admin, dict) else {}
    creds = parse_basic_auth(handler.headers.get("Authorization"))
    if creds:
        username, password = creds
        if constant_time_equal(username, admin.get("username", "admin")):
            password_hash = str(admin.get("password_hash") or "")
            if _verify_password(password, password_hash):
                if _password_hash_needs_upgrade(password_hash):
                    admin["password_hash"] = _hash_password(password)
                    try:
                        save_config(cfg, expected_revision=revision)
                    except Exception:
                        # A concurrent config change must not invalidate credentials
                        # already verified against the successfully loaded revision.
                        pass
                return True

    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="Gateway Admin"')
    send_cors_headers(handler)
    handler.end_headers()
    return False


def downstream_route(path: str) -> str:
    """Return the stable ACL capability name for a supported HTTP route."""
    if "/assistants" in path or "/threads" in path:
        return "assistants"
    if "/web2api" in path:
        return "web2api"
    if "/chat/completions" in path:
        return "chat_completions"
    if "/responses" in path:
        return "responses"
    if "/messages" in path:
        return "messages"
    if "/tools/call" in path or "/functions/call" in path:
        return "direct_tools"
    return "models"


def _downstream_api_key(handler: HTTPAuthHandler) -> str:
    auth = handler.headers.get("Authorization") or handler.headers.get("authorization")
    api_key = ""
    if auth:
        if auth.startswith("Bearer "):
            api_key = auth[7:]
        elif auth.startswith("Basic "):
            creds = parse_basic_auth(auth)
            if creds:
                api_key = creds[1]
    if not api_key:
        api_key = handler.headers.get("x-api-key") or handler.headers.get("X-API-Key") or ""
    return str(api_key)


def check_downstream_key(handler: HTTPAuthHandler) -> str | None:
    """Authenticate a downstream API key and enforce its route ACL."""
    from .gateway_config import _hash_secret, load_config

    cfg = load_config()
    raw_keys = cfg.get("downstream_keys")
    if raw_keys is None or raw_keys == []:
        return None
    if not isinstance(raw_keys, list):
        raise DownstreamAuthError("invalid downstream key configuration")
    downstream_keys = raw_keys

    api_key = _downstream_api_key(handler)
    if not api_key:
        raise DownstreamAuthError("missing Authorization or x-api-key header")

    key_hash = _hash_secret(api_key)
    route = downstream_route(handler.path)
    for entry in downstream_keys:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        if not constant_time_equal(entry.get("key_hash") or "", key_hash):
            continue
        protocols = set(entry.get("protocols") or [])
        if protocols:
            models_compatible = route == "models" and bool(
                protocols & {"models", "chat_completions", "responses", "messages"}
            )
            assistants_compatible = route == "assistants" and bool(
                protocols & {"assistants", "chat_completions", "responses", "messages"}
            )
            web2api_compatible = route == "web2api" and "direct_tools" in protocols
            if route not in protocols and not models_compatible and not assistants_compatible and not web2api_compatible:
                raise DownstreamAuthError(f"API key is not allowed for {route}")
        return str(entry.get("id") or entry.get("name") or "unknown")

    raise DownstreamAuthError("invalid API key")


_check_downstream_key = check_downstream_key


__all__ = [
    "AuthErrorHandler",
    "HTTPAuthHandler",
    "_check_downstream_key",
    "check_admin",
    "check_downstream_key",
    "downstream_route",
]
