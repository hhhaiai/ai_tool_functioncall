#!/usr/bin/env python3
"""HTTP Actions for the gateway.

Handles external HTTP action tools that can be configured via admin UI.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .gateway_errors import ToolExecutionError

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


_ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _stringify_action_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _expand_config_value(value: Any) -> str:
    return _ENV_TEMPLATE_RE.sub(lambda m: os.environ.get(m.group(1), ""), _stringify_action_value(value))


def _expand_action_value(value: Any) -> str:
    """Legacy export for config-template value expansion used by gateway_app."""
    return _expand_config_value(value)


def _http_action_headers(action: Json) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    action_headers = action.get("headers") or {}
    if isinstance(action_headers, dict):
        for key, value in action_headers.items():
            header_name = str(key).strip()
            if header_name:
                headers[header_name] = _expand_config_value(value)
    return headers


def _action_body(action: Json, arguments: Json) -> Json:
    body_template = action.get("body")
    if not isinstance(body_template, dict):
        return arguments
    body: Json = {}
    for key, value in body_template.items():
        if isinstance(value, str) and value.startswith("$") and not value.startswith("${"):
            param_name = value[1:]
            body[key] = arguments.get(param_name, value)
        else:
            body[key] = value
    return body


def _url_with_query(url: str, arguments: Json) -> str:
    if not arguments:
        return url
    parsed = urllib.parse.urlparse(url)
    existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query_items = existing + [(str(k), _stringify_action_value(v)) for k, v in arguments.items()]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query_items)))


def _validate_action_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolExecutionError("HTTP action url must be absolute http(s)", failure_type="invalid_input")


def _action_max_bytes(action: Json) -> int:
    try:
        value = int(action.get("max_bytes") or 1_000_000)
    except (TypeError, ValueError):
        value = 1_000_000
    return max(1, value)


def _read_limited_response(resp: Any, max_bytes: int) -> str:
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ToolExecutionError(
            f"HTTP action response exceeded max_bytes={max_bytes}",
            failure_type="response_too_large",
        )
    return data.decode("utf-8", errors="replace")


def _call_http_action(action: Json, arguments: Json) -> str:
    url = str(action.get("url") or "").strip()
    _validate_action_url(url)
    method = str(action.get("method") or "POST").upper()
    timeout = float(action.get("timeout") or 30)
    max_bytes = _action_max_bytes(action)
    headers = _http_action_headers(action)
    data = None
    if method in {"GET", "DELETE"}:
        url = _url_with_query(url, arguments)
    else:
        body = _action_body(action, arguments)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_data = _read_limited_response(resp, max_bytes)
            return f"status: {resp.status}\n\n{response_data}"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = _read_limited_response(e, max_bytes)
        except ToolExecutionError:
            raise
        except Exception:
            pass
        raise ToolExecutionError(f"HTTP {e.code}: {detail}", failure_type="http_action_failed") from e
    except urllib.error.URLError as e:
        raise ToolExecutionError(str(e.reason), failure_type="http_action_failed") from e
    except ToolExecutionError:
        raise
    except Exception as e:
        raise ToolExecutionError(str(e), failure_type="http_action_failed") from e
