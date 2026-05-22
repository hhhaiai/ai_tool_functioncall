#!/usr/bin/env python3
"""HTTP Actions for the gateway.

Handles external HTTP action tools that can be configured via admin UI.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

Json = dict[str, Any]


def _enabled_http_actions() -> list[Json]:
    from .gateway_config import load_config
    cfg = load_config()
    actions_cfg = cfg.get("http_actions", {})
    if not actions_cfg.get("enabled", True):
        return []
    actions = actions_cfg.get("actions") or []
    return [a for a in actions if isinstance(a, dict) and a.get("enabled", True)]


def _http_action_by_name(name: str) -> Json | None:
    for action in _enabled_http_actions():
        if action.get("name") == name:
            return action
    return None


def _http_action_schemas(path: str) -> list[Json]:
    schemas = []
    for action in _enabled_http_actions():
        name = action.get("name")
        if not name:
            continue
        schema = action.get("input_schema") or {
            "type": "object",
            "properties": {
                "arguments": {
                    "type": "object",
                    "description": "Arguments to pass to the HTTP action",
                },
            },
            "required": ["arguments"],
        }
        description = action.get("description") or f"HTTP action: {name}"
        if "/messages" in path:
            schemas.append({"name": name, "description": description, "input_schema": schema})
        else:
            schemas.append({
                "type": "function",
                "function": {"name": name, "description": description, "parameters": schema},
            })
    return schemas


def _expand_action_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _http_action_headers(action: Json) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    action_headers = action.get("headers") or {}
    if isinstance(action_headers, dict):
        headers.update(action_headers)
    return headers


def _call_http_action(action: Json, arguments: Json) -> str:
    url = action.get("url", "")
    method = str(action.get("method") or "POST").upper()
    timeout = float(action.get("timeout") or 30)
    headers = _http_action_headers(action)
    body_template = action.get("body")
    if body_template and isinstance(body_template, dict):
        body = {}
        for k, v in body_template.items():
            if isinstance(v, str) and v.startswith("$"):
                param_name = v[1:]
                body[k] = arguments.get(param_name, v)
            else:
                body[k] = v
    else:
        body = arguments
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_data = resp.read().decode("utf-8")
            return f"status: {resp.status}\n\n{response_data}"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return json.dumps({"error": f"HTTP {e.code}", "detail": detail}, ensure_ascii=False)
    except urllib.error.URLError as e:
        return json.dumps({"error": str(e.reason)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
