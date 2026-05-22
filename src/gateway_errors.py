#!/usr/bin/env python3
"""Error classes for the gateway.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class GatewayError(Exception):
    status = 500

    def __init__(self, message: str, *, detail: Any | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class UpstreamHTTPError(GatewayError):
    status = 502

    def __init__(self, upstream_status: int, detail: str) -> None:
        super().__init__(f"upstream HTTP {upstream_status}", detail=detail)
        self.upstream_status = upstream_status


class UpstreamTimeoutError(GatewayError):
    status = 504


class NativeToolVerificationError(GatewayError):
    status = 502


class DownstreamAuthError(GatewayError):
    status = 401


class GatewayBusyError(GatewayError):
    status = 429


class ToolExecutionError(Exception):
    def __init__(self, message: str, *, failure_type: str = "execution_failed") -> None:
        super().__init__(message)
        self.failure_type = failure_type


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    success: bool = True
    failure_type: str | None = None


def error_payload(message: str, *, detail: Any | None = None, upstream_status: int | None = None) -> dict[str, Any]:
    """Canonical error payload used by both HTTP handler and tool runtime."""
    payload: dict[str, Any] = {
        "error": {
            "message": message,
            "type": "gateway_error",
        }
    }
    if detail is not None:
        payload["error"]["detail"] = detail
    if upstream_status is not None:
        payload["error"]["upstream_status"] = upstream_status
    return payload
