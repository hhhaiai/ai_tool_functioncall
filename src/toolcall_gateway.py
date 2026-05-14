#!/usr/bin/env python3
"""Native tools/function-call gateway.

This server does NOT simulate tool calls with prompt JSON. It forwards native
`tools`, `tool_choice`, `tool_calls`, and Anthropic `tool_use` protocol objects
to an upstream provider that already supports them. If the upstream rejects or
fails a forced native tool call, the gateway fails fast instead of pretending.
"""

from __future__ import annotations

import argparse
import ast
import atexit
import base64
import copy
import datetime as _dt
import glob
import hashlib
import html
import json
import math
import os
import pathlib
import re
import select
import shlex
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

Json = dict[str, Any]

SUPPORTED_PATHS = {"/v1/chat/completions", "/v1/responses", "/v1/messages"}
DEFAULT_MAX_TOOL_ROUNDS = 5
CONFIG_PATH = pathlib.Path(os.environ.get("GATEWAY_CONFIG_PATH") or ".gateway_config.json")
REQUEST_LOG_PATH = pathlib.Path(os.environ.get("GATEWAY_REQUEST_LOG") or ".gateway_requests.jsonl")
STATS_PATH = pathlib.Path(os.environ.get("GATEWAY_STATS_PATH") or ".gateway_stats.json")
DEFAULT_DOWNSTREAM_KEY = "dev-gateway-key"
MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_CATALOG_CACHE_TTL_SECONDS = 60


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


class NativeToolVerificationError(GatewayError):
    status = 502


class DownstreamAuthError(GatewayError):
    status = 401


class ToolExecutionError(Exception):
    def __init__(self, message: str, *, failure_type: str = "execution_failed") -> None:
        super().__init__(message)
        self.failure_type = failure_type


@dataclass(frozen=True)
class GatewayTool:
    name: str
    description: str
    parameters: Json
    handler: Callable[[Json], str]
    risk: str = "pure"
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Json
    raw: Json


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    success: bool = True
    failure_type: str | None = None


class McpSession:
    def __init__(self, server: Json) -> None:
        self.server = copy.deepcopy(server)
        self.name = str(server.get("name") or "")
        self.timeout = float(server.get("timeout") or os.environ.get("GATEWAY_MCP_TIMEOUT", "20"))
        self.proc = _mcp_start(server)
        self.lock = threading.Lock()
        self.next_id = 1
        self.last_used_at = time.time()
        try:
            _mcp_initialize(self.proc, server, self.timeout, self._next_id_locked())
        except Exception:
            self.close()
            raise

    def _next_id_locked(self) -> int:
        request_id = self.next_id
        self.next_id += 1
        return request_id

    def request(self, method: str, params: Json | None = None) -> Json:
        with self.lock:
            if self.proc.poll() is not None:
                raise ToolExecutionError(f"MCP server {self.name} exited", failure_type="execution_failed")
            self.last_used_at = time.time()
            return _mcp_request(
                self.proc,
                method,
                params,
                request_id=self._next_id_locked(),
                timeout=self.timeout,
            )

    def close(self) -> None:
        proc = self.proc
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass


MCP_SESSIONS: dict[str, McpSession] = {}
MCP_SESSIONS_LOCK = threading.Lock()
MCP_TOOL_CATALOG_CACHE: dict[str, tuple[float, list[Json]]] = {}
MCP_SERVER_STATUS: dict[str, Json] = {}


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _default_config() -> Json:
    return {
        "admin": {
            "username": "admin",
            "password_hash": _hash_secret("admin"),
            "must_change_password": True,
        },
        "upstream": {
            "base_url": os.environ.get("UPSTREAM_BASE_URL", ""),
            "api_key": os.environ.get("UPSTREAM_API_KEY", ""),
            "model": os.environ.get("UPSTREAM_MODEL", ""),
            "protocol": os.environ.get("GATEWAY_UPSTREAM_PROTOCOL", "openai_chat"),
            "tools_enabled": os.environ.get("GATEWAY_TOOLS_ENABLED", "auto"),
            "native_tools_verified": False,
            "use_for_coding": True,
        },
        "gateway": {
            "tool_mode": os.environ.get("GATEWAY_TOOL_MODE", "orchestrate"),
            "max_tool_rounds": int(os.environ.get("GATEWAY_MAX_TOOL_ROUNDS") or DEFAULT_MAX_TOOL_ROUNDS),
            "workspace_root": os.environ.get("GATEWAY_WORKSPACE_ROOT") or os.getcwd(),
            "allow_write_tools": os.environ.get("GATEWAY_ALLOW_WRITE_TOOLS", "0") in {"1", "true", "yes"},
            "allow_shell_tools": os.environ.get("GATEWAY_ALLOW_SHELL_TOOLS", "0") in {"1", "true", "yes"},
            "request_logging": True,
        },
        "downstream_keys": [
            {
                "name": "default",
                "key_hash": _hash_secret(DEFAULT_DOWNSTREAM_KEY),
                "prefix": DEFAULT_DOWNSTREAM_KEY[:8],
                "enabled": True,
                "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }
        ],
        "mcp": {
            "servers": [],
            "marketplace_enabled": True,
        },
        "http_actions": {
            "enabled": True,
            "actions": [],
        },
    }


def load_config() -> Json:
    if not CONFIG_PATH.exists():
        cfg = _default_config()
        save_config(cfg)
        return cfg
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config root must be object")
    except Exception:
        loaded = {}
    cfg = _default_config()
    _deep_update(cfg, loaded)
    return cfg


def save_config(config: Json) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def _deep_update(base: Json, updates: Json) -> Json:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _redacted_config(config: Json) -> Json:
    redacted = _redact_payload(copy.deepcopy(config))
    if redacted.get("upstream", {}).get("api_key"):
        key = redacted["upstream"]["api_key"]
        redacted["upstream"]["api_key"] = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "***"
    for item in redacted.get("downstream_keys", []):
        item.pop("key_hash", None)
    return redacted


def _config_env(name: str, fallback: str = "") -> str:
    cfg = load_config()
    upstream = cfg.get("upstream", {})
    gateway = cfg.get("gateway", {})
    mapping = {
        "UPSTREAM_BASE_URL": upstream.get("base_url") or fallback,
        "UPSTREAM_API_KEY": upstream.get("api_key") or fallback,
        "UPSTREAM_MODEL": upstream.get("model") or fallback,
        "GATEWAY_TOOL_MODE": gateway.get("tool_mode") or fallback,
        "GATEWAY_MAX_TOOL_ROUNDS": str(gateway.get("max_tool_rounds") or fallback),
        "GATEWAY_WORKSPACE_ROOT": gateway.get("workspace_root") or fallback,
        "GATEWAY_ALLOW_WRITE_TOOLS": "1" if gateway.get("allow_write_tools") else fallback,
        "GATEWAY_ALLOW_SHELL_TOOLS": "1" if gateway.get("allow_shell_tools") else fallback,
    }
    return os.environ.get(name) or str(mapping.get(name) or fallback)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Json) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _read_json(handler: BaseHTTPRequestHandler) -> Json:
    length = int(handler.headers.get("content-length") or "0")
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise GatewayError("request body must be a JSON object")
    return parsed


def _has_requested_tools(body: Json) -> bool:
    tools = body.get("tools")
    return isinstance(tools, list) and bool(tools)


def _is_forced_tool_choice(path: str, body: Json) -> bool:
    choice = body.get("tool_choice")
    if not choice:
        return False
    if isinstance(choice, str):
        return choice not in {"auto", "none"}
    if isinstance(choice, dict):
        if path == "/v1/messages":
            return choice.get("type") in {"tool", "any"}
        return choice.get("type") in {"function", "tool", "required"} or "function" in choice
    return False


def _native_tool_signal(path: str, response: Json) -> bool:
    """Return true when a response contains real protocol-level tool-call data."""
    if path == "/v1/chat/completions":
        for choice in response.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if isinstance(message, dict) and message.get("tool_calls"):
                return True
            if choice.get("finish_reason") == "tool_calls":
                return True
        return False

    if path == "/v1/responses":
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in {"function_call", "tool_call", "computer_call", "file_search_call", "web_search_call"}:
                return True
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") in {"function_call", "tool_call"}:
                    return True
        return False

    if path == "/v1/messages":
        for block in response.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
        if response.get("stop_reason") == "tool_use":
            return True
        return False

    return False


def _workspace_root() -> pathlib.Path:
    return pathlib.Path(_config_env("GATEWAY_WORKSPACE_ROOT", os.getcwd())).resolve()


def _resolve_workspace_path(value: str | None, *, default: str = ".") -> pathlib.Path:
    raw = value or default
    candidate = pathlib.Path(raw)
    if not candidate.is_absolute():
        candidate = _workspace_root() / candidate
    resolved = candidate.resolve()
    root = _workspace_root()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolExecutionError(
            f"path escapes workspace root: {resolved}",
            failure_type="permission_denied",
        ) from exc
    return resolved


def _require_write_enabled() -> None:
    if _config_env("GATEWAY_ALLOW_WRITE_TOOLS", "0").lower() not in {"1", "true", "yes"}:
        raise ToolExecutionError(
            "write/edit tools are disabled; set GATEWAY_ALLOW_WRITE_TOOLS=1 to enable",
            failure_type="permission_denied",
        )


def _require_shell_enabled() -> None:
    if _config_env("GATEWAY_ALLOW_SHELL_TOOLS", "0").lower() not in {"1", "true", "yes"}:
        raise ToolExecutionError(
            "shell tools are disabled; set GATEWAY_ALLOW_SHELL_TOOLS=1 to enable",
            failure_type="permission_denied",
        )


def _json_schema(properties: Json, required: list[str] | None = None) -> Json:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": True,
    }


def _safe_calculate(expression: str) -> float | int:
    operators: dict[type[ast.AST], Callable[..., Any]] = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b,
        ast.FloorDiv: lambda a, b: a // b,
        ast.Mod: lambda a, b: a % b,
        ast.Pow: lambda a, b: a**b,
        ast.USub: lambda a: -a,
        ast.UAdd: lambda a: +a,
    }
    allowed_names = {"pi": math.pi, "e": math.e, "tau": math.tau}

    def eval_node(node: ast.AST) -> float | int:
        if isinstance(node, ast.Expression):
            return eval_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name) and node.id in allowed_names:
            return allowed_names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            return operators[type(node.op)](eval_node(node.left), eval_node(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in operators:
            return operators[type(node.op)](eval_node(node.operand))
        raise ToolExecutionError("calculator expression contains unsupported syntax", failure_type="invalid_input")

    return eval_node(ast.parse(expression, mode="eval"))


def _tool_echo_probe(args: Json) -> str:
    return str(args.get("value", ""))


def _tool_calculator(args: Json) -> str:
    expression = str(args.get("expression") or args.get("input") or "")
    if not expression.strip():
        raise ToolExecutionError("missing expression", failure_type="invalid_input")
    result = _safe_calculate(expression)
    if isinstance(result, float) and result.is_integer():
        return str(int(result))
    return str(result)


def _tool_current_time(args: Json) -> str:
    timezone = str(args.get("timezone") or "UTC").upper()
    now = _dt.datetime.now(_dt.timezone.utc)
    if timezone in {"LOCAL", "SYSTEM"}:
        now = _dt.datetime.now().astimezone()
    return now.isoformat()


def _tool_read(args: Json) -> str:
    path = _resolve_workspace_path(str(args.get("file_path") or args.get("path") or ""))
    if not path.is_file():
        raise ToolExecutionError(f"file not found: {path}", failure_type="not_found")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    offset = int(args.get("offset") or 1)
    limit = args.get("limit")
    start = max(offset - 1, 0)
    end = len(lines) if limit is None else min(start + int(limit), len(lines))
    numbered = [f"{idx + 1}: {line}" for idx, line in enumerate(lines[start:end], start=start)]
    return "\n".join(numbered)


def _tool_list_dir(args: Json) -> str:
    path = _resolve_workspace_path(str(args.get("path") or "."))
    if not path.is_dir():
        raise ToolExecutionError(f"directory not found: {path}", failure_type="not_found")
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        suffix = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{suffix}")
    return "\n".join(entries)


def _tool_glob(args: Json) -> str:
    pattern = str(args.get("pattern") or "**/*")
    base = _resolve_workspace_path(str(args.get("path") or "."))
    matches = []
    for match in glob.glob(str(base / pattern), recursive=True):
        path = pathlib.Path(match).resolve()
        try:
            rel = path.relative_to(_workspace_root())
        except ValueError:
            continue
        matches.append(str(rel) + ("/" if path.is_dir() else ""))
    return "\n".join(sorted(matches)[: int(args.get("limit") or 500)])


def _tool_grep(args: Json) -> str:
    pattern = str(args.get("pattern") or args.get("query") or "")
    if not pattern:
        raise ToolExecutionError("missing pattern", failure_type="invalid_input")
    base = _resolve_workspace_path(str(args.get("path") or "."))
    include = str(args.get("include") or args.get("glob") or "**/*")
    regex = re.compile(pattern)
    limit = int(args.get("limit") or 200)
    results: list[str] = []
    files = [pathlib.Path(p) for p in glob.glob(str(base / include), recursive=True)]
    for file_path in files:
        if len(results) >= limit:
            break
        if not file_path.is_file():
            continue
        try:
            file_path.resolve().relative_to(_workspace_root())
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rel = file_path.resolve().relative_to(_workspace_root())
                results.append(f"{rel}:{line_no}: {line}")
                if len(results) >= limit:
                    break
    return "\n".join(results)


def _tool_write(args: Json) -> str:
    _require_write_enabled()
    path = _resolve_workspace_path(str(args.get("file_path") or args.get("path") or ""))
    content = str(args.get("content") if args.get("content") is not None else args.get("file_text") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} bytes to {path.relative_to(_workspace_root())}"


def _tool_edit(args: Json) -> str:
    _require_write_enabled()
    path = _resolve_workspace_path(str(args.get("file_path") or args.get("path") or ""))
    old = str(args.get("old_string") or args.get("old") or "")
    new = str(args.get("new_string") or args.get("new") or "")
    replace_all = bool(args.get("replace_all"))
    if not old:
        raise ToolExecutionError("missing old_string", failure_type="invalid_input")
    text = path.read_text(encoding="utf-8", errors="replace")
    if old not in text:
        raise ToolExecutionError("old_string not found", failure_type="not_found")
    count = text.count(old) if replace_all else 1
    path.write_text(text.replace(old, new, -1 if replace_all else 1), encoding="utf-8")
    return f"edited {path.relative_to(_workspace_root())}; replacements={count}"


def _tool_multiedit(args: Json) -> str:
    _require_write_enabled()
    path = _resolve_workspace_path(str(args.get("file_path") or args.get("path") or ""))
    edits = args.get("edits")
    if not isinstance(edits, list):
        raise ToolExecutionError("missing edits list", failure_type="invalid_input")
    text = path.read_text(encoding="utf-8", errors="replace")
    count = 0
    for edit in edits:
        if not isinstance(edit, dict):
            raise ToolExecutionError("each edit must be an object", failure_type="invalid_input")
        old = str(edit.get("old_string") or edit.get("old") or "")
        new = str(edit.get("new_string") or edit.get("new") or "")
        replace_all = bool(edit.get("replace_all"))
        if old not in text:
            raise ToolExecutionError(f"old_string not found for edit {count + 1}", failure_type="not_found")
        text = text.replace(old, new, -1 if replace_all else 1)
        count += 1
    path.write_text(text, encoding="utf-8")
    return f"applied {count} edits to {path.relative_to(_workspace_root())}"


def _tool_shell(args: Json) -> str:
    _require_shell_enabled()
    command = str(args.get("command") or args.get("cmd") or "")
    if not command:
        raise ToolExecutionError("missing command", failure_type="invalid_input")
    timeout = float(args.get("timeout") or os.environ.get("GATEWAY_SHELL_TIMEOUT", "30"))
    cwd = _resolve_workspace_path(str(args.get("cwd") or args.get("workdir") or "."))
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        shell=True,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = []
    output.append(f"exit_code={completed.returncode}")
    if completed.stdout:
        output.append("stdout:\n" + completed.stdout)
    if completed.stderr:
        output.append("stderr:\n" + completed.stderr)
    return "\n".join(output)


def _tool_apply_patch(args: Json) -> str:
    _require_write_enabled()
    patch = str(args.get("patch") or args.get("input") or args.get("diff") or "")
    if not patch.strip():
        raise ToolExecutionError("missing patch", failure_type="invalid_input")
    apply_patch_bin = os.environ.get("GATEWAY_APPLY_PATCH_BIN") or "apply_patch"
    completed = subprocess.run(
        [apply_patch_bin],
        input=patch,
        cwd=str(_workspace_root()),
        text=True,
        capture_output=True,
        timeout=float(os.environ.get("GATEWAY_APPLY_PATCH_TIMEOUT", "30")),
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise ToolExecutionError(output.strip() or "apply_patch failed", failure_type="execution_failed")
    return output.strip() or "patch applied"


def _tool_fetch_url(args: Json) -> str:
    url = str(args.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise ToolExecutionError("url must start with http:// or https://", failure_type="invalid_input")
    timeout = float(args.get("timeout") or os.environ.get("GATEWAY_FETCH_TIMEOUT", "20"))
    req = urllib.request.Request(url, headers={"user-agent": "ToolCallGateway/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(int(args.get("max_bytes") or 200_000))
        content_type = resp.headers.get("content-type", "")
    text = data.decode("utf-8", errors="replace")
    return f"content-type: {content_type}\n\n{text}"


def _tool_todo_write(args: Json) -> str:
    todos = args.get("todos") or args.get("items") or []
    if not isinstance(todos, list):
        raise ToolExecutionError("todos must be a list", failure_type="invalid_input")
    return json.dumps({"ok": True, "todos": todos}, ensure_ascii=False)


def _tool_update_plan(args: Json) -> str:
    plan = args.get("plan") or args.get("items") or []
    return json.dumps({"ok": True, "plan": plan}, ensure_ascii=False)


def _tool_connector_required(args: Json) -> str:
    name = str(args.get("_tool_name") or "tool")
    raise ToolExecutionError(
        f"{name} requires a configured connector/runtime and is not ready",
        failure_type="connector_required",
    )


def _make_tools() -> dict[str, GatewayTool]:
    path_props = {
        "file_path": {"type": "string"},
        "path": {"type": "string"},
        "offset": {"type": "integer"},
        "limit": {"type": "integer"},
    }
    tools = [
        GatewayTool(
            "echo_probe",
            "Return the input value. Used to verify real native tool calling.",
            _json_schema({"value": {"type": "string"}}, ["value"]),
            _tool_echo_probe,
            aliases=("gateway__echo_probe",),
        ),
        GatewayTool(
            "calculator",
            "Safely evaluate a basic arithmetic expression.",
            _json_schema({"expression": {"type": "string"}}, ["expression"]),
            _tool_calculator,
            aliases=("gateway__calculator",),
        ),
        GatewayTool(
            "get_current_time",
            "Return the current time as ISO-8601.",
            _json_schema({"timezone": {"type": "string"}}),
            _tool_current_time,
            aliases=("gateway__get_current_time", "current_time"),
        ),
        GatewayTool("Read", "Read a text file from the workspace.", _json_schema(path_props), _tool_read, "read_local", aliases=("read_file", "FileReadTool")),
        GatewayTool("LS", "List a workspace directory.", _json_schema({"path": {"type": "string"}}), _tool_list_dir, "read_local", aliases=("list_dir",)),
        GatewayTool("Glob", "Find files by glob pattern in the workspace.", _json_schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer"}}, ["pattern"]), _tool_glob, "read_local", aliases=("glob_files", "find_files")),
        GatewayTool("Grep", "Search workspace files with a regular expression.", _json_schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}, "limit": {"type": "integer"}}, ["pattern"]), _tool_grep, "read_local", aliases=("grep_files", "file_search")),
        GatewayTool("Write", "Write a file in the workspace. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"file_path": {"type": "string"}, "content": {"type": "string"}}, ["file_path", "content"]), _tool_write, "write_local", aliases=("write_file",)),
        GatewayTool("Edit", "Replace text in a workspace file. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}}, ["file_path", "old_string", "new_string"]), _tool_edit, "write_local", aliases=("edit_file",)),
        GatewayTool("MultiEdit", "Apply multiple string replacements to a workspace file. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"file_path": {"type": "string"}, "edits": {"type": "array"}}, ["file_path", "edits"]), _tool_multiedit, "write_local"),
        GatewayTool("Bash", "Run a shell command in the workspace. Disabled unless GATEWAY_ALLOW_SHELL_TOOLS=1.", _json_schema({"command": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "number"}}, ["command"]), _tool_shell, "execute_code", aliases=("exec_command", "shell_command", "exec_shell")),
        GatewayTool("apply_patch", "Apply a Codex-style patch in the workspace. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"patch": {"type": "string"}}, ["patch"]), _tool_apply_patch, "write_local"),
        GatewayTool("WebFetch", "Fetch a URL over HTTP(S).", _json_schema({"url": {"type": "string"}, "max_bytes": {"type": "integer"}}, ["url"]), _tool_fetch_url, "read_network", aliases=("fetch_url",)),
        GatewayTool("TodoWrite", "Accept and persist a todo list in the conversation result.", _json_schema({"todos": {"type": "array"}}, ["todos"]), _tool_todo_write, "state", aliases=("todo_write",)),
        GatewayTool("update_plan", "Accept a plan/update_plan payload.", _json_schema({"plan": {"type": "array"}, "explanation": {"type": "string"}}), _tool_update_plan, "state"),
    ]
    connector_required = [
        "WebSearch",
        "web_search",
        "Task",
        "Agent",
        "spawn_agent",
        "wait_agent",
        "close_agent",
        "request_user_input",
        "AskUserQuestion",
        "Skill",
        "NotebookEdit",
        "ListMcpResourcesTool",
        "ReadMcpResourceTool",
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
        "view_image",
        "write_stdin",
        "exec_shell_wait",
        "exec_shell_interact",
        "exec_wait",
        "exec_interact",
    ]
    registry: dict[str, GatewayTool] = {}
    for tool in tools:
        registry[tool.name] = tool
        for alias in tool.aliases:
            registry[alias] = tool
    for name in connector_required:
        registry[name] = GatewayTool(
            name,
            f"{name} compatibility placeholder; requires marketplace/MCP/plugin connector.",
            _json_schema({"_tool_name": {"type": "string"}}),
            lambda args, tool_name=name: _tool_connector_required({**args, "_tool_name": tool_name}),
            "connector_required",
        )
    return registry


BUILTIN_TOOLS = _make_tools()


def _tool_name_from_schema(path: str, item: Json) -> str | None:
    if path == "/v1/messages":
        return item.get("name") if isinstance(item.get("name"), str) else None
    if item.get("type") == "function" and isinstance(item.get("function"), dict):
        return item["function"].get("name")
    return item.get("name") if isinstance(item.get("name"), str) else None


def _tool_schema_for_path(path: str, tool: GatewayTool) -> Json:
    if path == "/v1/messages":
        return {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}
    if path == "/v1/responses":
        return {"type": "function", "name": tool.name, "description": tool.description, "parameters": tool.parameters}
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
    }


def _mcp_public_name(server_name: str, tool_name: str) -> str:
    safe_server = re.sub(r"[^A-Za-z0-9_-]+", "_", server_name).strip("_") or "mcp"
    safe_tool = re.sub(r"[^A-Za-z0-9_-]+", "_", tool_name).strip("_") or "tool"
    return f"mcp__{safe_server}__{safe_tool}"


def _mcp_parse_public_name(name: str) -> tuple[str, str] | None:
    if not name.startswith("mcp__"):
        return None
    parts = name.split("__", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def _enabled_mcp_servers() -> list[Json]:
    servers = load_config().get("mcp", {}).get("servers", [])
    if not isinstance(servers, list):
        return []
    return [s for s in servers if isinstance(s, dict) and s.get("enabled", True)]


def _mcp_server_by_name(name: str) -> Json | None:
    for server in _enabled_mcp_servers():
        if str(server.get("name") or "") == name:
            return server
    return None


def _mcp_env(server: Json) -> dict[str, str]:
    env = os.environ.copy()
    raw_env = server.get("env")
    if isinstance(raw_env, dict):
        env.update({str(k): str(v) for k, v in raw_env.items()})
    elif isinstance(raw_env, list):
        for key in raw_env:
            key = str(key)
            if key in os.environ:
                env[key] = os.environ[key]
    return env


def _mcp_command(server: Json) -> list[str]:
    if isinstance(server.get("command"), list):
        return [str(x) for x in server["command"]]
    command = str(server.get("command") or "")
    if not command:
        raise ToolExecutionError("MCP server command is required", failure_type="invalid_input")
    args = server.get("args") or []
    if isinstance(args, str):
        args_list = shlex.split(args)
    elif isinstance(args, list):
        args_list = [str(x) for x in args]
    else:
        args_list = []
    return [command, *args_list]


def _mcp_write_message(proc: subprocess.Popen, message: Json) -> None:
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
    assert proc.stdin is not None
    proc.stdin.write(header + raw)
    proc.stdin.flush()


def _mcp_read_exact(stream: Any, length: int, timeout: float) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        ready, _, _ = select.select([stream], [], [], timeout)
        if not ready:
            raise ToolExecutionError("MCP server response timed out", failure_type="timeout")
        chunk = stream.read(remaining)
        if not chunk:
            raise ToolExecutionError("MCP server closed stdout", failure_type="execution_failed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _mcp_read_message(proc: subprocess.Popen, timeout: float) -> Json:
    assert proc.stdout is not None
    header = b""
    while b"\r\n\r\n" not in header and b"\n\n" not in header:
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            raise ToolExecutionError("MCP server response timed out", failure_type="timeout")
        byte = proc.stdout.read(1)
        if not byte:
            raise ToolExecutionError("MCP server closed stdout", failure_type="execution_failed")
        header += byte
        if len(header) > 8192:
            raise ToolExecutionError("MCP response header too large", failure_type="execution_failed")
    if b"\r\n\r\n" in header:
        header_bytes, rest = header.split(b"\r\n\r\n", 1)
    else:
        header_bytes, rest = header.split(b"\n\n", 1)
    content_length = None
    for line in header_bytes.decode("ascii", errors="replace").splitlines():
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break
    if content_length is None:
        # Some lightweight test servers use newline-delimited JSON.
        line = header.strip()
        try:
            parsed = json.loads(line.decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        raise ToolExecutionError("MCP response missing Content-Length", failure_type="execution_failed")
    body = rest + _mcp_read_exact(proc.stdout, content_length - len(rest), timeout)
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ToolExecutionError("MCP response must be JSON object", failure_type="execution_failed")
    return parsed


def _mcp_request(proc: subprocess.Popen, method: str, params: Json | None = None, *, request_id: int = 1, timeout: float = 20) -> Json:
    payload: Json = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    _mcp_write_message(proc, payload)
    while True:
        response = _mcp_read_message(proc, timeout)
        if response.get("id") != request_id:
            continue
        if "error" in response:
            raise ToolExecutionError(f"MCP {method} failed: {response['error']}", failure_type="execution_failed")
        result = response.get("result") or {}
        if not isinstance(result, dict):
            raise ToolExecutionError(f"MCP {method} result must be object", failure_type="execution_failed")
        return result


def _mcp_notify(proc: subprocess.Popen, method: str, params: Json | None = None) -> None:
    payload: Json = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    _mcp_write_message(proc, payload)


def _mcp_start(server: Json) -> subprocess.Popen:
    command = _mcp_command(server)
    cwd = str(_resolve_workspace_path(str(server.get("cwd") or ".")))
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=_mcp_env(server),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def _mcp_initialize(proc: subprocess.Popen, server: Json, timeout: float, request_id: int = 1) -> None:
    _mcp_request(
        proc,
        "initialize",
        {
            "protocolVersion": str(server.get("protocolVersion") or MCP_PROTOCOL_VERSION),
            "capabilities": {},
            "clientInfo": {"name": "toolcall-gateway", "version": "0.1"},
        },
        request_id=request_id,
        timeout=timeout,
    )
    _mcp_notify(proc, "notifications/initialized")


def _mcp_with_server(server: Json, fn: Callable[[subprocess.Popen, float], Any]) -> Any:
    timeout = float(server.get("timeout") or os.environ.get("GATEWAY_MCP_TIMEOUT", "20"))
    proc = _mcp_start(server)
    try:
        _mcp_initialize(proc, server, timeout)
        return fn(proc, timeout)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass


def _mcp_session_key(server: Json) -> str:
    return str(server.get("name") or json.dumps(_mcp_command(server), sort_keys=True))


def _mcp_use_pool(server: Json) -> bool:
    return bool(server.get("pool", True))


def _mcp_get_session(server: Json) -> McpSession:
    key = _mcp_session_key(server)
    with MCP_SESSIONS_LOCK:
        session = MCP_SESSIONS.get(key)
        if session and session.proc.poll() is None:
            return session
        if session:
            session.close()
        session = McpSession(server)
        MCP_SESSIONS[key] = session
        return session


def _mcp_close_sessions() -> None:
    with MCP_SESSIONS_LOCK:
        sessions = list(MCP_SESSIONS.values())
        MCP_SESSIONS.clear()
    for session in sessions:
        session.close()
    MCP_TOOL_CATALOG_CACHE.clear()
    MCP_SERVER_STATUS.clear()


def _mcp_catalog_ttl(server: Json) -> float:
    return float(server.get("catalog_ttl") or os.environ.get("GATEWAY_MCP_CATALOG_TTL") or MCP_CATALOG_CACHE_TTL_SECONDS)


def _mcp_cache_key(server: Json) -> str:
    return _mcp_session_key(server)


def _mcp_set_status(server_name: str, status: str, *, detail: str | None = None, tool_count: int | None = None) -> None:
    payload = MCP_SERVER_STATUS.setdefault(server_name, {})
    payload.update(
        {
            "name": server_name,
            "status": status,
            "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
    )
    if detail is not None:
        payload["detail"] = detail
    if tool_count is not None:
        payload["tool_count"] = tool_count


def _mcp_invalidate_server(server: Json, *, reason: str | None = None) -> None:
    key = _mcp_session_key(server)
    with MCP_SESSIONS_LOCK:
        session = MCP_SESSIONS.pop(key, None)
    if session:
        session.close()
    MCP_TOOL_CATALOG_CACHE.pop(key, None)
    if reason:
        _mcp_set_status(key, "restarting", detail=reason)


def _mcp_health_snapshot(*, probe: bool = False) -> list[Json]:
    rows: list[Json] = []
    for server in _enabled_mcp_servers():
        name = str(server.get("name") or _mcp_session_key(server))
        session = MCP_SESSIONS.get(name)
        cached = MCP_TOOL_CATALOG_CACHE.get(name)
        base = {
            "name": name,
            "enabled": True,
            "session": "connected" if session and session.proc.poll() is None else "not_connected",
            "cache": "hit" if cached and cached[0] > time.time() else "miss",
            "cached_tool_count": len(cached[1]) if cached else 0,
        }
        base.update(MCP_SERVER_STATUS.get(name, {}))
        if probe:
            try:
                tools = _mcp_list_server_tools(server)
                base.update({"status": "ready", "tool_count": len(tools), "detail": ""})
            except Exception as exc:
                base.update({"status": "broken", "detail": str(exc)})
        rows.append(base)
    return rows


atexit.register(_mcp_close_sessions)


def _mcp_list_server_tools(server: Json) -> list[Json]:
    key = _mcp_cache_key(server)
    now = time.time()
    ttl = _mcp_catalog_ttl(server)
    cached = MCP_TOOL_CATALOG_CACHE.get(key)
    if cached and cached[0] > now:
        _mcp_set_status(key, "ready", tool_count=len(cached[1]))
        return copy.deepcopy(cached[1])

    try:
        if _mcp_use_pool(server):
            result = _mcp_get_session(server).request("tools/list", {})
        else:
            def run(proc: subprocess.Popen, timeout: float) -> Json:
                return _mcp_request(proc, "tools/list", {}, request_id=2, timeout=timeout)

            result = _mcp_with_server(server, run)
        tools = [t for t in (result.get("tools") or []) if isinstance(t, dict) and t.get("name")]
        MCP_TOOL_CATALOG_CACHE[key] = (now + ttl, copy.deepcopy(tools))
        _mcp_set_status(key, "ready", tool_count=len(tools), detail="")
        return tools
    except Exception as exc:
        _mcp_invalidate_server(server, reason=str(exc))
        _mcp_set_status(key, "broken", detail=str(exc), tool_count=0)
        raise


def _mcp_call_tool(server: Json, tool_name: str, arguments: Json) -> str:
    key = _mcp_session_key(server)
    try:
        if _mcp_use_pool(server):
            result = _mcp_get_session(server).request("tools/call", {"name": tool_name, "arguments": arguments})
        else:
            def run(proc: subprocess.Popen, timeout: float) -> Json:
                return _mcp_request(
                    proc,
                    "tools/call",
                    {"name": tool_name, "arguments": arguments},
                    request_id=2,
                    timeout=timeout,
                )

            result = _mcp_with_server(server, run)
        if result.get("isError"):
            raise ToolExecutionError(_mcp_content_to_text(result), failure_type="execution_failed")
        _mcp_set_status(key, "ready", detail="")
        return _mcp_content_to_text(result)
    except Exception as exc:
        _mcp_invalidate_server(server, reason=str(exc))
        _mcp_set_status(key, "broken", detail=str(exc))
        raise


def _mcp_content_to_text(result: Json) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return json.dumps(result, ensure_ascii=False)
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(str(item))
        elif item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(part for part in parts if part)


def _mcp_tool_schemas(path: str) -> list[Json]:
    schemas: list[Json] = []
    for server in _enabled_mcp_servers():
        server_name = str(server.get("name") or "")
        if not server_name:
            continue
        try:
            for tool in _mcp_list_server_tools(server):
                gateway_tool = GatewayTool(
                    name=_mcp_public_name(server_name, str(tool["name"])),
                    description=str(tool.get("description") or f"MCP tool {server_name}/{tool['name']}"),
                    parameters=tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else _json_schema({}),
                    handler=lambda _args: "",
                    risk="mcp",
                )
                schemas.append(_tool_schema_for_path(path, gateway_tool))
        except Exception as exc:
            call = ToolCall(
                call_id=f"mcp_list_{server_name}",
                name=f"mcp::{server_name}::tools/list",
                arguments={},
                raw={},
            )
            result = ToolResult(
                call_id=call.call_id,
                name=call.name,
                content=f"connector_required: {exc}",
                success=False,
                failure_type="connector_required",
            )
            _record_tool_failure(call, result)
    return schemas


def _enabled_http_actions() -> list[Json]:
    actions_cfg = load_config().get("http_actions", {})
    if not isinstance(actions_cfg, dict) or not actions_cfg.get("enabled", True):
        return []
    actions = actions_cfg.get("actions", [])
    if not isinstance(actions, list):
        return []
    return [
        action
        for action in actions
        if isinstance(action, dict)
        and action.get("enabled", True)
        and isinstance(action.get("name"), str)
        and action.get("name")
    ]


def _http_action_by_name(name: str) -> Json | None:
    for action in _enabled_http_actions():
        aliases = action.get("aliases") if isinstance(action.get("aliases"), list) else []
        if action.get("name") == name or name in aliases:
            return action
    return None


def _http_action_schemas(path: str) -> list[Json]:
    schemas: list[Json] = []
    for action in _enabled_http_actions():
        gateway_tool = GatewayTool(
            name=str(action["name"]),
            description=str(action.get("description") or f"HTTP action {action['name']}"),
            parameters=action.get("input_schema") if isinstance(action.get("input_schema"), dict) else _json_schema({}),
            handler=lambda _args: "",
            risk="http_action",
        )
        schemas.append(_tool_schema_for_path(path, gateway_tool))
    return schemas


def _expand_action_value(value: Any) -> str:
    text = str(value)
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text)
    if match:
        return os.environ.get(match.group(1), "")
    return text


def _http_action_headers(action: Json) -> dict[str, str]:
    headers = {"user-agent": "ToolCallGateway/1.0"}
    raw_headers = action.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise ToolExecutionError("http action headers must be an object", failure_type="invalid_input")
    for key, value in raw_headers.items():
        headers[str(key)] = _expand_action_value(value)
    return headers


def _call_http_action(action: Json, arguments: Json) -> str:
    url = str(action.get("url") or "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolExecutionError("http action url must be an absolute http(s) URL", failure_type="invalid_input")
    method = str(action.get("method") or "POST").upper()
    timeout = float(action.get("timeout") or os.environ.get("GATEWAY_HTTP_ACTION_TIMEOUT", "30"))
    max_bytes = int(action.get("max_bytes") or os.environ.get("GATEWAY_HTTP_ACTION_MAX_BYTES", "200000"))
    headers = _http_action_headers(action)
    data: bytes | None = None
    request_url = url
    if method in {"GET", "DELETE"}:
        query = urllib.parse.urlencode({str(k): v for k, v in arguments.items()}, doseq=True)
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        request_url = f"{url}{sep}{query}" if query else url
    else:
        data = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
        headers.setdefault("content-type", "application/json")
    req = urllib.request.Request(request_url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes)
            content_type = resp.headers.get("content-type", "")
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read(max_bytes).decode("utf-8", errors="replace")
        raise ToolExecutionError(f"http action returned {exc.code}: {detail}", failure_type="execution_failed") from exc
    except urllib.error.URLError as exc:
        raise ToolExecutionError(f"http action connection failed: {exc.reason}", failure_type="execution_failed") from exc
    text = body.decode("utf-8", errors="replace")
    if content_type.startswith("application/json"):
        try:
            parsed_body = json.loads(text)
            text = json.dumps(parsed_body, ensure_ascii=False)
        except Exception:
            pass
    return f"status: {status}\ncontent-type: {content_type}\n\n{text}"


def _merge_builtin_tools(path: str, body: Json) -> Json:
    if os.environ.get("GATEWAY_EXPOSE_BUILTIN_TOOLS", "1").lower() in {"0", "false", "no"}:
        return body
    merged = dict(body)
    tools = list(merged.get("tools") or [])
    existing = {_tool_name_from_schema(path, t) for t in tools if isinstance(t, dict)}
    for name, tool in BUILTIN_TOOLS.items():
        if name != tool.name:
            continue
        if tool.risk == "connector_required" and os.environ.get("GATEWAY_EXPOSE_CONNECTOR_PLACEHOLDERS", "0") not in {"1", "true", "yes"}:
            continue
        if tool.name not in existing:
            tools.append(_tool_schema_for_path(path, tool))
            existing.add(tool.name)
    if load_config().get("mcp", {}).get("enabled", True):
        for schema in _mcp_tool_schemas(path):
            name = _tool_name_from_schema(path, schema)
            if name and name not in existing:
                tools.append(schema)
                existing.add(name)
    for schema in _http_action_schemas(path):
        name = _tool_name_from_schema(path, schema)
        if name and name not in existing:
            tools.append(schema)
            existing.add(name)
    merged["tools"] = tools
    return merged


def _copy_model_override(body: Json) -> Json:
    copied = dict(body)
    model = _config_env("UPSTREAM_MODEL", "")
    if model:
        copied["model"] = model
    return copied


class NativeProxyClient:
    def __init__(self) -> None:
        self.base_url = _config_env("UPSTREAM_BASE_URL", "").rstrip("/")
        self.api_key = _config_env("UPSTREAM_API_KEY", "")
        self.anthropic_version = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")
        self.timeout = float(os.environ.get("UPSTREAM_TIMEOUT", "60"))
        if not self.base_url:
            raise GatewayError("UPSTREAM_BASE_URL is required")

    def forward(self, path: str, body: Json) -> Json:
        payload = _copy_model_override(body)
        return self._post(path, payload)

    def _post(self, path: str, body: Json) -> Json:
        headers = self._headers(path)
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise UpstreamHTTPError(exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise GatewayError(f"upstream connection failed: {exc.reason}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GatewayError("upstream returned non-JSON response", detail=raw[:2000]) from exc
        if not isinstance(parsed, dict):
            raise GatewayError("upstream returned non-object JSON", detail=parsed)
        return parsed

    def _headers(self, path: str) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if path == "/v1/messages":
            if self.api_key:
                headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = self.anthropic_version
            beta = os.environ.get("ANTHROPIC_BETA")
            if beta:
                headers["anthropic-beta"] = beta
        elif self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers


def _parse_json_arguments(raw: Any) -> Json:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ToolExecutionError(f"tool arguments are not valid JSON: {exc}", failure_type="invalid_input") from exc
        if isinstance(parsed, dict):
            return parsed
        raise ToolExecutionError("tool arguments JSON must decode to an object", failure_type="invalid_input")
    raise ToolExecutionError("tool arguments must be an object or JSON string", failure_type="invalid_input")


def _extract_tool_calls(path: str, response: Json) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if path == "/v1/chat/completions":
        for choice in response.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                if not isinstance(fn, dict) or not fn.get("name"):
                    continue
                calls.append(
                    ToolCall(
                        call_id=str(call.get("id") or f"call_{uuid.uuid4().hex}"),
                        name=str(fn["name"]),
                        arguments=_parse_json_arguments(fn.get("arguments")),
                        raw=call,
                    )
                )
        return calls

    if path == "/v1/responses":
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call" and item.get("name"):
                calls.append(
                    ToolCall(
                        call_id=str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"),
                        name=str(item["name"]),
                        arguments=_parse_json_arguments(item.get("arguments")),
                        raw=item,
                    )
                )
        return calls

    if path == "/v1/messages":
        for block in response.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name"):
                calls.append(
                    ToolCall(
                        call_id=str(block.get("id") or f"toolu_{uuid.uuid4().hex}"),
                        name=str(block["name"]),
                        arguments=_parse_json_arguments(block.get("input") or {}),
                        raw=block,
                    )
                )
        return calls

    return calls


def _assistant_message_from_chat_response(response: Json) -> Json:
    choices = response.get("choices") or []
    if choices and isinstance(choices[0], dict) and isinstance(choices[0].get("message"), dict):
        return dict(choices[0]["message"])
    return {"role": "assistant", "content": None}


def _append_tool_results(path: str, body: Json, response: Json, results: list[ToolResult]) -> Json:
    updated = dict(body)
    if path == "/v1/chat/completions":
        messages = list(updated.get("messages") or [])
        messages.append(_assistant_message_from_chat_response(response))
        for result in results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
                }
            )
        updated["messages"] = messages
        return updated

    if path == "/v1/responses":
        existing = updated.get("input")
        if isinstance(existing, list):
            input_items = list(existing)
        elif existing is None:
            input_items = []
        else:
            input_items = [{"role": "user", "content": existing}]
        for item in response.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "function_call":
                input_items.append(item)
        for result in results:
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": result.call_id,
                    "output": result.content,
                }
            )
        updated["input"] = input_items
        return updated

    if path == "/v1/messages":
        messages = list(updated.get("messages") or [])
        content = response.get("content") or []
        messages.append({"role": "assistant", "content": content})
        result_blocks = [
            {
                "type": "tool_result",
                "tool_use_id": result.call_id,
                "content": result.content,
                **({"is_error": True} if not result.success else {}),
            }
            for result in results
        ]
        messages.append({"role": "user", "content": result_blocks})
        updated["messages"] = messages
        return updated

    return updated


def _failure_log_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("GATEWAY_TOOL_FAILURE_LOG") or ".gateway_tool_failures.jsonl")


def _record_tool_failure(call: ToolCall, result: ToolResult) -> None:
    if result.success:
        return
    event = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "tool_name": call.name,
        "call_id": call.call_id,
        "failure_type": result.failure_type,
        "arguments_keys": sorted(call.arguments.keys()),
        "content": result.content[:1000],
        "fake_prompt_tools": False,
    }
    try:
        with _failure_log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        if os.environ.get("DEBUG"):
            traceback.print_exc()


def _read_json_file(path: pathlib.Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        if os.environ.get("DEBUG"):
            traceback.print_exc()
    return copy.deepcopy(default)


def _write_json_file(path: pathlib.Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_tool_stat(name: str, success: bool, failure_type: str | None = None) -> None:
    stats = _read_json_file(STATS_PATH, {"tools": {}, "requests": {"total": 0}})
    tools = stats.setdefault("tools", {})
    item = tools.setdefault(name, {"calls": 0, "success": 0, "failure": 0, "failures": {}})
    item["calls"] += 1
    if success:
        item["success"] += 1
    else:
        item["failure"] += 1
        failures = item.setdefault("failures", {})
        failures[failure_type or "unknown"] = failures.get(failure_type or "unknown", 0) + 1
    item["last_called_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _write_json_file(STATS_PATH, stats)


def _record_request_stat(path: str, status: int) -> None:
    stats = _read_json_file(STATS_PATH, {"tools": {}, "requests": {"total": 0}})
    requests = stats.setdefault("requests", {"total": 0})
    requests["total"] = requests.get("total", 0) + 1
    by_path = requests.setdefault("by_path", {})
    by_path[path] = by_path.get(path, 0) + 1
    by_status = requests.setdefault("by_status", {})
    status_key = str(status)
    by_status[status_key] = by_status.get(status_key, 0) + 1
    requests["last_request_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _write_json_file(STATS_PATH, stats)


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if key.lower() in {"authorization", "api_key", "x-api-key", "key", "token", "password", "secret"}:
                out[key] = "***"
            else:
                out[key] = _redact_payload(val)
        return out
    if isinstance(value, list):
        return [_redact_payload(v) for v in value]
    return value


def _write_request_log(path: str, body: Json, status: int, response: Json | None, downstream_key: str | None) -> None:
    if not load_config().get("gateway", {}).get("request_logging", True):
        return
    event = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "request_id": f"req_{uuid.uuid4().hex}",
        "path": path,
        "status": status,
        "downstream_key": downstream_key,
        "request": _redact_payload(body),
        "response": _redact_payload(response) if response is not None else None,
        "fake_prompt_tools": False,
    }
    with REQUEST_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _tail_jsonl(path: pathlib.Path, limit: int = 50) -> list[Json]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    rows = []
    for line in lines:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except Exception:
            continue
    return rows


def _text_response(handler: BaseHTTPRequestHandler, status: int, payload: str, content_type: str = "text/html; charset=utf-8") -> None:
    data = payload.encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", content_type)
    handler.send_header("content-length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header or not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except Exception:
        return None


def _check_admin(handler: BaseHTTPRequestHandler) -> bool:
    cfg = load_config()
    parsed = _parse_basic_auth(handler.headers.get("authorization"))
    admin = cfg.get("admin", {})
    if parsed and parsed[0] == admin.get("username", "admin") and _hash_secret(parsed[1]) == admin.get("password_hash"):
        return True
    handler.send_response(401)
    handler.send_header("www-authenticate", 'Basic realm="Gateway Admin"')
    handler.send_header("content-type", "text/plain; charset=utf-8")
    handler.end_headers()
    handler.wfile.write(b"admin authentication required")
    return False


def _check_downstream_key(handler: BaseHTTPRequestHandler) -> str | None:
    cfg = load_config()
    keys = cfg.get("downstream_keys") or []
    if not keys:
        return "no-key-configured"
    auth = handler.headers.get("authorization") or ""
    supplied = ""
    if auth.startswith("Bearer "):
        supplied = auth.split(" ", 1)[1].strip()
    elif handler.headers.get("x-api-key"):
        supplied = handler.headers.get("x-api-key", "").strip()
    if not supplied:
        raise DownstreamAuthError("missing downstream API key")
    supplied_hash = _hash_secret(supplied)
    for item in keys:
        if item.get("enabled", True) and item.get("key_hash") == supplied_hash:
            return str(item.get("name") or item.get("prefix") or "key")
    raise DownstreamAuthError("invalid downstream API key")


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("content-length") or "0")
    raw = handler.rfile.read(length).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
    return {k: v[-1] if v else "" for k, v in parsed.items()}


def _render_admin_ui() -> str:
    cfg = load_config()
    redacted = _redacted_config(cfg)
    stats = _read_json_file(STATS_PATH, {"tools": {}, "requests": {}})
    failures = _tail_jsonl(_failure_log_path(), 20)
    requests = _tail_jsonl(REQUEST_LOG_PATH, 20)
    upstream = cfg.get("upstream", {})
    gateway = cfg.get("gateway", {})
    tool_rows = "\n".join(
        f"<tr><td>{html.escape(name)}</td><td>{item.get('calls', 0)}</td><td>{item.get('success', 0)}</td><td>{item.get('failure', 0)}</td><td><code>{html.escape(json.dumps(item.get('failures', {}), ensure_ascii=False))}</code></td></tr>"
        for name, item in sorted((stats.get("tools") or {}).items())
    )
    key_rows = "\n".join(
        f"<tr><td>{html.escape(str(k.get('name')))}</td><td>{html.escape(str(k.get('prefix')))}</td><td>{'yes' if k.get('enabled', True) else 'no'}</td></tr>"
        for k in cfg.get("downstream_keys", [])
    )
    failure_rows = "\n".join(
        f"<tr><td>{html.escape(str(x.get('ts')))}</td><td>{html.escape(str(x.get('tool_name')))}</td><td>{html.escape(str(x.get('failure_type')))}</td><td><code>{html.escape(str(x.get('content')))}</code></td></tr>"
        for x in failures
    )
    request_rows = "\n".join(
        f"<tr><td>{html.escape(str(x.get('ts')))}</td><td>{html.escape(str(x.get('path')))}</td><td>{x.get('status')}</td><td>{html.escape(str(x.get('downstream_key')))}</td></tr>"
        for x in requests
    )
    mcp_json = html.escape(json.dumps(cfg.get("mcp", {}).get("servers", []), ensure_ascii=False, indent=2))
    http_actions_json = html.escape(json.dumps(cfg.get("http_actions", {}).get("actions", []), ensure_ascii=False, indent=2))
    mcp_session_count = len(MCP_SESSIONS)
    mcp_cache_count = len(MCP_TOOL_CATALOG_CACHE)
    mcp_health_rows = "\n".join(
        f"<tr><td>{html.escape(str(row.get('name')))}</td><td>{html.escape(str(row.get('status', 'unknown')))}</td><td>{html.escape(str(row.get('session')))}</td><td>{html.escape(str(row.get('cache')))}</td><td>{row.get('tool_count', row.get('cached_tool_count', 0))}</td><td><code>{html.escape(str(row.get('detail', '')))}</code></td></tr>"
        for row in _mcp_health_snapshot(probe=False)
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Gateway Admin</title>
<style>body{{font-family:system-ui;margin:24px;max-width:1200px}} input,select,textarea{{width:100%;box-sizing:border-box;margin:4px 0 10px;padding:8px}} table{{border-collapse:collapse;width:100%;margin:12px 0}} td,th{{border:1px solid #ddd;padding:6px;vertical-align:top}} code,pre{{background:#f6f6f6;padding:2px 4px}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}} button{{padding:8px 14px}}</style>
</head><body>
<h1>Tool Call Gateway Admin</h1>
<p>默认管理员：<code>admin/admin</code>；默认下游 key：<code>{DEFAULT_DOWNSTREAM_KEY}</code>。上线前请修改。</p>
<div class="grid">
<section><h2>上游 API</h2>
<form method="post" action="/admin/config">
<label>Base URL</label><input name="base_url" value="{html.escape(str(upstream.get('base_url','')))}">
<label>API Key（留空则不修改）</label><input name="api_key" type="password" placeholder="keep unchanged">
<label>Model</label><input name="model" value="{html.escape(str(upstream.get('model','')))}">
<label>Protocol</label><select name="protocol">
{''.join(f'<option value="{p}" {"selected" if upstream.get("protocol") == p else ""}>{p}</option>' for p in ["openai_chat","openai_responses","anthropic_messages","openai_compatible"])}
</select>
<label>Tools Enabled</label><select name="tools_enabled">
{''.join(f'<option value="{p}" {"selected" if upstream.get("tools_enabled") == p else ""}>{p}</option>' for p in ["auto","on","off","native_only"])}
</select>
<label><input type="checkbox" name="native_tools_verified" value="1" {"checked" if upstream.get("native_tools_verified") else ""} style="width:auto"> Native tools 已验证</label>
<label><input type="checkbox" name="use_for_coding" value="1" {"checked" if upstream.get("use_for_coding", True) else ""} style="width:auto"> 用于 coding agent</label>
<h3>Gateway Runtime</h3>
<label>Tool Mode</label><select name="tool_mode">
{''.join(f'<option value="{p}" {"selected" if gateway.get("tool_mode") == p else ""}>{p}</option>' for p in ["orchestrate","passthrough"])}
</select>
<label>Max Tool Rounds</label><input name="max_tool_rounds" value="{html.escape(str(gateway.get('max_tool_rounds', DEFAULT_MAX_TOOL_ROUNDS)))}">
<label>Workspace Root</label><input name="workspace_root" value="{html.escape(str(gateway.get('workspace_root','')))}">
<label><input type="checkbox" name="allow_write_tools" value="1" {"checked" if gateway.get("allow_write_tools") else ""} style="width:auto"> 允许写入工具</label>
<label><input type="checkbox" name="allow_shell_tools" value="1" {"checked" if gateway.get("allow_shell_tools") else ""} style="width:auto"> 允许 Shell 工具</label>
<label><input type="checkbox" name="request_logging" value="1" {"checked" if gateway.get("request_logging", True) else ""} style="width:auto"> 保留下游请求和响应</label>
<button>保存配置</button>
</form></section>
<section><h2>下游 API Keys</h2>
<table><tr><th>Name</th><th>Prefix</th><th>Enabled</th></tr>{key_rows}</table>
<form method="post" action="/admin/downstream-key">
<label>Name</label><input name="name" placeholder="codex-local">
<label>Key</label><input name="key" placeholder="sk-local-...">
<button>添加/更新 Key</button>
</form>
<h2>修改管理员密码</h2>
<form method="post" action="/admin/password">
<label>New password</label><input type="password" name="password">
<button>修改密码</button>
</form></section>
</div>
<section><h2>本地 MCP / Connector Catalog</h2>
<form method="post" action="/admin/mcp">
<textarea name="servers" rows="8">{mcp_json}</textarea>
<button>保存 MCP 配置</button>
</form>
<form method="post" action="/admin/mcp-reload"><button>刷新 MCP 连接和工具缓存</button></form>
<p>当前已支持 stdio MCP <code>initialize</code> / <code>tools/list</code> / <code>tools/call</code>，ready tools 会以 <code>mcp__server__tool</code> 形式自动暴露。</p>
<p>MCP sessions: <code>{mcp_session_count}</code>，catalog cache: <code>{mcp_cache_count}</code>。查看 <code>/admin/mcp-tools.json</code>。</p>
<table><tr><th>Server</th><th>Status</th><th>Session</th><th>Cache</th><th>Tools</th><th>Detail</th></tr>{mcp_health_rows}</table>
</section>
<section><h2>HTTP Actions</h2>
<form method="post" action="/admin/http-actions">
<textarea name="actions" rows="8">{http_actions_json}</textarea>
<button>保存 HTTP Actions</button>
</form>
<p>HTTP action 会作为真实 tool/function executor 暴露，默认直接使用 action <code>name</code>。POST/PUT/PATCH 会把工具参数作为 JSON body；GET/DELETE 会把参数放到 query。</p>
<p>示例：<code>{{"name":"lookup_user","method":"POST","url":"http://127.0.0.1:9000/lookup","input_schema":{{"type":"object","properties":{{"id":{{"type":"string"}}}}}}}}</code></p>
</section>
<section><h2>Tool 调用频次</h2><table><tr><th>Tool</th><th>Calls</th><th>Success</th><th>Failure</th><th>Failures</th></tr>{tool_rows}</table></section>
<section><h2>失败/不支持 Function Calls / Tool Calls</h2><table><tr><th>Time</th><th>Tool</th><th>Type</th><th>Content</th></tr>{failure_rows}</table><p>这些会进入 marketplace/backlog 搜索与后续实现。</p></section>
<section><h2>最近下游请求</h2><table><tr><th>Time</th><th>Path</th><th>Status</th><th>Key</th></tr>{request_rows}</table></section>
<section><h2>当前配置（脱敏）</h2><pre>{html.escape(json.dumps(redacted, ensure_ascii=False, indent=2))}</pre></section>
</body></html>"""


def _redirect(handler: BaseHTTPRequestHandler, location: str = "/ui") -> None:
    handler.send_response(303)
    handler.send_header("location", location)
    handler.end_headers()


def _execute_tool_call(call: ToolCall) -> ToolResult:
    tool = BUILTIN_TOOLS.get(call.name)
    mcp_target = _mcp_parse_public_name(call.name)
    if mcp_target:
        server_name, mcp_tool_name = mcp_target
        server = _mcp_server_by_name(server_name)
        if not server:
            result = ToolResult(
                call_id=call.call_id,
                name=call.name,
                content=f"connector_required: MCP server {server_name} is not configured or enabled",
                success=False,
                failure_type="connector_required",
            )
            _record_tool_failure(call, result)
            _record_tool_stat(call.name, False, "connector_required")
            return result
        try:
            content = _mcp_call_tool(server, mcp_tool_name, call.arguments)
            _record_tool_stat(call.name, True)
            return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
        except ToolExecutionError as exc:
            result = ToolResult(
                call_id=call.call_id,
                name=call.name,
                content=f"{exc.failure_type}: {exc}",
                success=False,
                failure_type=exc.failure_type,
            )
            _record_tool_failure(call, result)
            _record_tool_stat(call.name, False, exc.failure_type)
            return result
    http_action = _http_action_by_name(call.name)
    if http_action:
        try:
            content = _call_http_action(http_action, call.arguments)
            _record_tool_stat(call.name, True)
            return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
        except ToolExecutionError as exc:
            result = ToolResult(
                call_id=call.call_id,
                name=call.name,
                content=f"{exc.failure_type}: {exc}",
                success=False,
                failure_type=exc.failure_type,
            )
            _record_tool_failure(call, result)
            _record_tool_stat(call.name, False, exc.failure_type)
            return result
    if not tool:
        result = ToolResult(
            call_id=call.call_id,
            name=call.name,
            content=f"ToolNotFound: {call.name} is not implemented or installed in Gateway runtime",
            success=False,
            failure_type="tool_not_found",
        )
        _record_tool_failure(call, result)
        _record_tool_stat(call.name, False, "tool_not_found")
        return result
    try:
        content = tool.handler(call.arguments)
        _record_tool_stat(call.name, True)
        return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
    except ToolExecutionError as exc:
        result = ToolResult(
            call_id=call.call_id,
            name=call.name,
            content=f"{exc.failure_type}: {exc}",
            success=False,
            failure_type=exc.failure_type,
        )
        _record_tool_failure(call, result)
        _record_tool_stat(call.name, False, exc.failure_type)
        return result
    except subprocess.TimeoutExpired as exc:
        result = ToolResult(
            call_id=call.call_id,
            name=call.name,
            content=f"timeout: tool execution exceeded {exc.timeout}s",
            success=False,
            failure_type="timeout",
        )
        _record_tool_failure(call, result)
        _record_tool_stat(call.name, False, "timeout")
        return result
    except Exception as exc:
        result = ToolResult(
            call_id=call.call_id,
            name=call.name,
            content=f"execution_failed: {exc}",
            success=False,
            failure_type="execution_failed",
        )
        _record_tool_failure(call, result)
        _record_tool_stat(call.name, False, "execution_failed")
        return result


def run_tool_orchestration(path: str, body: Json, client: NativeProxyClient | None = None) -> Json:
    mode = _config_env("GATEWAY_TOOL_MODE", "orchestrate").lower()
    if mode in {"passthrough", "native_passthrough", "proxy"}:
        response = (client or NativeProxyClient()).forward(path, body)
        _verify_native_if_forced(path, body, response)
        return response

    max_rounds = int(_config_env("GATEWAY_MAX_TOOL_ROUNDS", str(DEFAULT_MAX_TOOL_ROUNDS)))
    upstream = client or NativeProxyClient()
    request_body = _merge_builtin_tools(path, body)
    for _round in range(max_rounds):
        response = upstream.forward(path, request_body)
        _verify_native_if_forced(path, request_body, response)
        calls = _extract_tool_calls(path, response)
        if not calls:
            return response
        results = [_execute_tool_call(call) for call in calls]
        request_body = _append_tool_results(path, request_body, response, results)
    raise GatewayError("max tool rounds exceeded", detail={"max_tool_rounds": max_rounds})


def _error_payload(message: str, *, detail: Any | None = None, upstream_status: int | None = None) -> Json:
    payload: Json = {
        "error": {
            "message": message,
            "type": "native_tool_gateway_error",
            "fake_prompt_tools": False,
        }
    }
    if detail is not None:
        payload["error"]["detail"] = detail
    if upstream_status is not None:
        payload["error"]["upstream_status"] = upstream_status
    return payload


def _verify_native_if_forced(path: str, body: Json, response: Json) -> None:
    if not os.environ.get("NATIVE_TOOLS_STRICT", "1").lower() not in {"0", "false", "no"}:
        return
    if _has_requested_tools(body) and _is_forced_tool_choice(path, body) and not _native_tool_signal(path, response):
        raise NativeToolVerificationError(
            "forced native tool call did not return protocol-level tool call data; "
            "upstream is not confirmed native-tools capable for this request",
            detail={"path": path, "tool_choice": body.get("tool_choice")},
        )


def _probe_body(path: str, model: str | None) -> Json:
    model_name = model or os.environ.get("UPSTREAM_MODEL") or "native-tool-probe-model"
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string", "description": "return the literal probe value"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    if path == "/v1/messages":
        return {
            "model": model_name,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "Use echo_probe with value native_probe."}],
            "tools": [{"name": "echo_probe", "description": "native tool probe", "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": "echo_probe"},
        }
    if path == "/v1/responses":
        return {
            "model": model_name,
            "input": "Use echo_probe with value native_probe.",
            "tools": [{"type": "function", "name": "echo_probe", "description": "native tool probe", "parameters": schema}],
            "tool_choice": {"type": "function", "name": "echo_probe"},
        }
    return {
        "model": model_name,
        "messages": [{"role": "user", "content": "Use echo_probe with value native_probe."}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "echo_probe", "description": "native tool probe", "parameters": schema},
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "echo_probe"}},
    }


def run_native_probe(path: str, model: str | None = None) -> Json:
    if path not in SUPPORTED_PATHS:
        raise GatewayError(f"unsupported probe path: {path}")
    body = _probe_body(path, model)
    response = NativeProxyClient().forward(path, body)
    ok = _native_tool_signal(path, response)
    return {
        "ok": ok,
        "path": path,
        "native_tool_signal": ok,
        "fake_prompt_tools": False,
        "request_tool_choice": body.get("tool_choice"),
        "response": response,
    }


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "NativeToolGateway/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "mode": os.environ.get("GATEWAY_TOOL_MODE", "orchestrate"),
                    "fake_prompt_tools": False,
                    "supported_paths": sorted(SUPPORTED_PATHS),
                    "builtin_tool_count": len({tool.name for tool in BUILTIN_TOOLS.values()}),
                },
            )
            return
        if path in {"/", "/ui"}:
            if not _check_admin(self):
                return
            _text_response(self, 200, _render_admin_ui())
            return
        if path == "/admin/config.json":
            if not _check_admin(self):
                return
            _json_response(self, 200, {"config": _redacted_config(load_config())})
            return
        if path == "/admin/stats.json":
            if not _check_admin(self):
                return
            _json_response(self, 200, {"stats": _read_json_file(STATS_PATH, {"tools": {}, "requests": {}})})
            return
        if path == "/admin/requests.json":
            if not _check_admin(self):
                return
            _json_response(self, 200, {"requests": _tail_jsonl(REQUEST_LOG_PATH, 200)})
            return
        if path == "/admin/failures.json":
            if not _check_admin(self):
                return
            _json_response(self, 200, {"failures": _tail_jsonl(_failure_log_path(), 200)})
            return
        if path == "/admin/mcp-tools.json":
            if not _check_admin(self):
                return
            tools: list[Json] = []
            for server in _enabled_mcp_servers():
                server_name = str(server.get("name") or "")
                try:
                    for tool in _mcp_list_server_tools(server):
                        tools.append(
                            {
                                "server": server_name,
                                "name": tool.get("name"),
                                "gateway_name": _mcp_public_name(server_name, str(tool.get("name"))),
                                "description": tool.get("description"),
                            }
                        )
                except Exception as exc:
                    tools.append({"server": server_name, "error": str(exc)})
            _json_response(self, 200, {"tools": tools})
            return
        if path == "/admin/mcp-health.json":
            if not _check_admin(self):
                return
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            probe = query.get("probe", ["0"])[0] in {"1", "true", "yes"}
            _json_response(self, 200, {"servers": _mcp_health_snapshot(probe=probe)})
            return
        if path == "/admin/http-actions.json":
            if not _check_admin(self):
                return
            actions = []
            for action in _enabled_http_actions():
                actions.append(
                    {
                        "name": action.get("name"),
                        "method": str(action.get("method") or "POST").upper(),
                        "url": action.get("url"),
                        "description": action.get("description"),
                        "enabled": action.get("enabled", True),
                    }
                )
            _json_response(self, 200, {"actions": actions})
            return
        _json_response(self, 404, _error_payload("not found"))

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = self.path.split("?", 1)[0]
            if path in {"/admin/config", "/admin/password", "/admin/downstream-key", "/admin/mcp", "/admin/mcp-reload", "/admin/http-actions"}:
                if not _check_admin(self):
                    return
                form = _read_form(self)
                cfg = load_config()
                if path == "/admin/mcp-reload":
                    _mcp_close_sessions()
                elif path == "/admin/config":
                    cfg["upstream"]["base_url"] = form.get("base_url", "").strip()
                    if form.get("api_key"):
                        cfg["upstream"]["api_key"] = form["api_key"].strip()
                    cfg["upstream"]["model"] = form.get("model", "").strip()
                    cfg["upstream"]["protocol"] = form.get("protocol", "openai_chat")
                    cfg["upstream"]["tools_enabled"] = form.get("tools_enabled", "auto")
                    cfg["upstream"]["native_tools_verified"] = form.get("native_tools_verified") == "1"
                    cfg["upstream"]["use_for_coding"] = form.get("use_for_coding") == "1"
                    cfg["gateway"]["tool_mode"] = form.get("tool_mode", "orchestrate")
                    cfg["gateway"]["max_tool_rounds"] = int(form.get("max_tool_rounds") or DEFAULT_MAX_TOOL_ROUNDS)
                    cfg["gateway"]["workspace_root"] = form.get("workspace_root") or os.getcwd()
                    cfg["gateway"]["allow_write_tools"] = form.get("allow_write_tools") == "1"
                    cfg["gateway"]["allow_shell_tools"] = form.get("allow_shell_tools") == "1"
                    cfg["gateway"]["request_logging"] = form.get("request_logging") == "1"
                    save_config(cfg)
                elif path == "/admin/password":
                    password = form.get("password", "")
                    if len(password) < 6:
                        _text_response(self, 400, "password must be at least 6 chars", "text/plain; charset=utf-8")
                        return
                    cfg["admin"]["password_hash"] = _hash_secret(password)
                    cfg["admin"]["must_change_password"] = False
                    save_config(cfg)
                elif path == "/admin/downstream-key":
                    name = form.get("name", "").strip() or f"key-{uuid.uuid4().hex[:6]}"
                    key = form.get("key", "").strip()
                    if len(key) < 8:
                        _text_response(self, 400, "key must be at least 8 chars", "text/plain; charset=utf-8")
                        return
                    existing = [k for k in cfg.get("downstream_keys", []) if k.get("name") != name]
                    existing.append(
                        {
                            "name": name,
                            "key_hash": _hash_secret(key),
                            "prefix": key[:8],
                            "enabled": True,
                            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                        }
                    )
                    cfg["downstream_keys"] = existing
                    save_config(cfg)
                elif path == "/admin/mcp":
                    raw = form.get("servers", "[]")
                    try:
                        servers = json.loads(raw)
                        if not isinstance(servers, list):
                            raise ValueError("servers must be a list")
                    except Exception as exc:
                        _text_response(self, 400, f"invalid mcp json: {exc}", "text/plain; charset=utf-8")
                        return
                    cfg.setdefault("mcp", {})["servers"] = servers
                    save_config(cfg)
                    _mcp_close_sessions()
                elif path == "/admin/http-actions":
                    raw = form.get("actions", "[]")
                    try:
                        actions = json.loads(raw)
                        if not isinstance(actions, list):
                            raise ValueError("actions must be a list")
                    except Exception as exc:
                        _text_response(self, 400, f"invalid http actions json: {exc}", "text/plain; charset=utf-8")
                        return
                    cfg.setdefault("http_actions", {})["actions"] = actions
                    cfg.setdefault("http_actions", {})["enabled"] = True
                    save_config(cfg)
                _redirect(self)
                return

            body = _read_json(self)

            if path == "/v1/native-tools/probe":
                downstream_key = _check_downstream_key(self)
                probe_path = str(body.get("path") or "/v1/chat/completions")
                response = run_native_probe(probe_path, body.get("model"))
                _record_request_stat(path, 200)
                _write_request_log(path, body, 200, response, downstream_key)
                _json_response(self, 200, response)
                return

            if path not in SUPPORTED_PATHS:
                _json_response(self, 404, _error_payload("not found"))
                return

            downstream_key = _check_downstream_key(self)

            if body.get("stream"):
                raise GatewayError(
                    "stream passthrough is not implemented yet; refusing to fake native streaming tool events"
                )

            response = run_tool_orchestration(path, body)
            _record_request_stat(path, 200)
            _write_request_log(path, body, 200, response, downstream_key)
            _json_response(self, 200, response)
        except UpstreamHTTPError as exc:
            _record_request_stat(self.path.split("?", 1)[0], exc.status)
            _json_response(
                self,
                exc.status,
                _error_payload(
                    "upstream rejected the native request; no prompt-based fake fallback was used",
                    detail=exc.detail,
                    upstream_status=exc.upstream_status,
                ),
            )
        except GatewayError as exc:
            _record_request_stat(self.path.split("?", 1)[0], exc.status)
            _json_response(self, exc.status, _error_payload(str(exc), detail=exc.detail))
        except Exception as exc:
            if os.environ.get("DEBUG"):
                traceback.print_exc()
            _record_request_stat(self.path.split("?", 1)[0], 500)
            _json_response(self, 500, _error_payload(str(exc)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a native tools/function-call runtime gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    print(f"native tool runtime gateway listening on http://{args.host}:{args.port}", flush=True)
    print("fake prompt tools: disabled", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
