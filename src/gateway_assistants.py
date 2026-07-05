#!/usr/bin/env python3
"""Gateway-owned compatibility handlers for Assistants/Threads endpoints.

Weak chat-only upstreams generally do not implement OpenAI Assistants or Threads.
If the gateway advertises these base paths, it must not forward them as chat
requests and then fail with upstream schema errors.  These helpers provide a
small, deterministic compatibility surface for exact create requests.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

Json = dict[str, Any]


def _now() -> int:
    return int(time.time())


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _metadata(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def create_assistant_response(body: Json) -> Json:
    """Return an OpenAI-compatible Assistant object without upstream I/O."""
    from .gateway_config import _upstream_config

    upstream_model = str(_upstream_config().get("model") or "")
    model = body.get("model") or upstream_model or "gateway-default"
    response: Json = {
        "id": _id("asst"),
        "object": "assistant",
        "created_at": _now(),
        "name": body.get("name"),
        "description": body.get("description"),
        "model": model,
        "instructions": body.get("instructions"),
        "tools": body.get("tools") if isinstance(body.get("tools"), list) else [],
        "metadata": _metadata(body.get("metadata")),
        "response_format": body.get("response_format", "auto"),
    }
    if "temperature" in body:
        response["temperature"] = body.get("temperature")
    if "top_p" in body:
        response["top_p"] = body.get("top_p")
    if "tool_resources" in body:
        response["tool_resources"] = body.get("tool_resources") if isinstance(body.get("tool_resources"), dict) else {}
    return response


def create_thread_response(body: Json) -> Json:
    """Return an OpenAI-compatible Thread object without upstream I/O."""
    messages = body.get("messages")
    response: Json = {
        "id": _id("thread"),
        "object": "thread",
        "created_at": _now(),
        "metadata": _metadata(body.get("metadata")),
        "tool_resources": body.get("tool_resources") if isinstance(body.get("tool_resources"), dict) else {},
    }
    # The official create-thread response is a Thread object and does not echo
    # messages, but exposing a small count is useful for gateway audit/debug and
    # does not leak content across tenants/workspaces.
    if isinstance(messages, list):
        response["gateway_message_count"] = len(messages)
    return response


def handle_assistants_or_threads(path: str, body: Json) -> Json | None:
    """Return a gateway-owned response for exact base endpoints, else None."""
    if path == "/v1/assistants":
        return create_assistant_response(body)
    if path == "/v1/threads":
        return create_thread_response(body)
    return None
