"""Small, typed HTTP request/response I/O primitives for the Gateway."""
from __future__ import annotations

import base64
import hmac
import json
import urllib.parse
from typing import Any, BinaryIO, Mapping, Protocol

from .gateway_errors import BadRequestError, RequestBodyTooLargeError
from .gateway_http_security import send_cors_headers

Json = dict[str, Any]


class HTTPIOHandler(Protocol):
    """Structural subset of ``BaseHTTPRequestHandler`` used by these helpers."""

    headers: Mapping[str, str]
    rfile: BinaryIO
    wfile: BinaryIO

    def send_response(self, code: int, message: str | None = None) -> None: ...

    def send_header(self, keyword: str, value: str) -> None: ...

    def end_headers(self) -> None: ...


def json_response(
    handler: HTTPIOHandler,
    status: int,
    payload: Json,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    send_cors_headers(handler)
    for key, value in (headers or {}).items():
        handler.send_header(str(key), str(value))
    handler.end_headers()
    handler.wfile.write(body)


def safe_json_response(
    handler: HTTPIOHandler,
    status: int,
    payload: Json,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    """Best-effort JSON response used from exception handlers."""
    try:
        json_response(handler, status, payload, headers=headers)
    except Exception:
        try:
            handler.send_response(500)
            handler.end_headers()
        except Exception:
            pass


def text_response(
    handler: HTTPIOHandler,
    status: int,
    payload: str,
    content_type: str = "text/html; charset=utf-8",
) -> None:
    body = payload.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    send_cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def request_body_limit() -> int:
    from .gateway_config import _gateway_config

    try:
        value = int(_gateway_config().get("max_request_body_bytes") or 64 * 1024 * 1024)
    except (TypeError, ValueError):
        value = 64 * 1024 * 1024
    return max(1, value)


def request_content_length(handler: HTTPIOHandler) -> int:
    raw = handler.headers.get("Content-Length", "0")
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError) as exc:
        raise BadRequestError("invalid Content-Length header") from exc


def read_limited_body(
    handler: HTTPIOHandler,
    *,
    content_length: int | None = None,
    limit: int | None = None,
) -> bytes:
    expected = request_content_length(handler) if content_length is None else max(0, int(content_length))
    if expected == 0:
        return b""
    maximum = request_body_limit() if limit is None else max(1, int(limit))
    if expected > maximum:
        raise RequestBodyTooLargeError(
            f"request body too large: {expected} bytes exceeds limit {maximum}"
        )
    body = handler.rfile.read(expected)
    if len(body) != expected:
        raise BadRequestError(
            "incomplete request body",
            detail={"expected_bytes": expected, "received_bytes": len(body)},
        )
    return body


def decode_json_object(raw: bytes) -> Json:
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadRequestError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BadRequestError("invalid JSON: request body must be an object")
    return value


def read_json(handler: HTTPIOHandler) -> Json:
    return decode_json_object(read_limited_body(handler))


def decode_form(
    raw: bytes,
    *,
    content_type: str = "",
    max_fields: int = 1000,
) -> dict[str, str]:
    """Decode an Admin JSON or URL-encoded form with bounded field count."""
    if not raw:
        return {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadRequestError(f"invalid form UTF-8: {exc}") from exc

    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if media_type == "application/json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BadRequestError(f"invalid JSON form: {exc}") from exc
        if not isinstance(value, dict):
            raise BadRequestError("invalid JSON form: request body must be an object")
        return {str(key): str(item) for key, item in value.items()}

    try:
        parsed = urllib.parse.parse_qs(
            text,
            keep_blank_values=False,
            strict_parsing=False,
            max_num_fields=max(1, int(max_fields)),
        )
    except ValueError as exc:
        raise BadRequestError(f"invalid form data: {exc}") from exc
    return {str(key): str(values[0]) if values else "" for key, values in parsed.items()}


def read_form(handler: HTTPIOHandler, *, max_fields: int = 1000) -> dict[str, str]:
    return decode_form(
        read_limited_body(handler),
        content_type=handler.headers.get("Content-Type", ""),
        max_fields=max_fields,
    )


def constant_time_equal(left: object, right: object) -> bool:
    """Compare auth material without timing leaks or Unicode type errors."""
    return hmac.compare_digest(str(left).encode("utf-8"), str(right).encode("utf-8"))


def parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header or not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        if ":" in decoded:
            username, password = decoded.split(":", 1)
            return username, password
    except Exception:
        pass
    return None


# Backward-compatible private spellings used by gateway_http_handler/gateway_app.
_json_response = json_response
_safe_json_response = safe_json_response
_text_response = text_response
_request_body_limit = request_body_limit
_request_content_length = request_content_length
_read_limited_body = read_limited_body
_decode_json_object = decode_json_object
_decode_form = decode_form
_read_json = read_json
_read_form = read_form
_constant_time_equal = constant_time_equal
_parse_basic_auth = parse_basic_auth


__all__ = [
    "HTTPIOHandler",
    "_constant_time_equal",
    "_decode_json_object",
    "_decode_form",
    "_json_response",
    "_parse_basic_auth",
    "_read_json",
    "_read_form",
    "_read_limited_body",
    "_request_body_limit",
    "_request_content_length",
    "_safe_json_response",
    "_text_response",
    "constant_time_equal",
    "decode_json_object",
    "decode_form",
    "json_response",
    "parse_basic_auth",
    "read_json",
    "read_form",
    "read_limited_body",
    "request_body_limit",
    "request_content_length",
    "safe_json_response",
    "text_response",
]
