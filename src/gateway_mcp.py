#!/usr/bin/env python3
"""MCP (Model Context Protocol) server management.

Handles MCP server connections, tool discovery, and tool execution.
"""
from __future__ import annotations

import copy
import json
import os
import re
import select
import subprocess
import threading
import time
from typing import Any, Callable

Json = dict[str, Any]

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_CATALOG_CACHE_TTL_SECONDS = 60
MCP_SESSIONS: dict[str, "McpSession"] = {}
MCP_SESSIONS_LOCK = threading.Lock()
MCP_TOOL_CATALOG_CACHE: dict[str, tuple[float, list[Json]]] = {}
MCP_SERVER_STATUS: dict[str, Json] = {}


class ToolExecutionError(Exception):
    def __init__(self, message: str, *, failure_type: str = "execution_failed") -> None:
        super().__init__(message)
        self.failure_type = failure_type


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


def _mcp_safe_component(value: str, *, default: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-._")
    return cleaned or default


def _mcp_public_name(server_name: str, tool_name: str) -> str:
    safe_server = _mcp_safe_component(server_name, default="server")
    safe_tool = _mcp_safe_component(tool_name, default="tool")
    return f"mcp__{safe_server}__{safe_tool}"


def _mcp_legacy_public_name(server_name: str, tool_name: str) -> str:
    return f"mcp_{server_name}_{tool_name}"


def _mcp_parse_public_name(name: str) -> tuple[str, str] | None:
    # Only parse the unambiguous mcp__server__tool format. The older
    # mcp_server_tool schema name is still exposed for client compatibility but
    # is not parsed here because server/tool boundaries are ambiguous.
    if name.startswith("mcp__"):
        parts = name[5:].split("__", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
    return None


def _enabled_mcp_servers() -> list[Json]:
    from .gateway_config import load_config
    cfg = load_config()
    mcp_cfg = cfg.get("mcp", {})
    servers = mcp_cfg.get("servers") or []
    return [s for s in servers if isinstance(s, dict) and s.get("enabled", True)]


def _mcp_server_by_name(name: str) -> Json | None:
    for server in _enabled_mcp_servers():
        if server.get("name") == name:
            return server
    return None


def _mcp_env(server: Json) -> dict[str, str]:
    env = dict(os.environ)
    server_env = server.get("env") or {}
    if isinstance(server_env, dict):
        env.update(server_env)
    return env


def _mcp_command(server: Json) -> list[str]:
    command = server.get("command")
    if isinstance(command, str):
        import shlex
        parts = shlex.split(command)
    elif isinstance(command, list):
        parts = [str(c) for c in command]
    else:
        raise ToolExecutionError("MCP server missing 'command'", failure_type="invalid_input")
    args = server.get("args") or []
    if isinstance(args, str):
        import shlex
        parts.extend(shlex.split(args))
    elif isinstance(args, list):
        parts.extend(str(arg) for arg in args)
    return parts


def _mcp_write_message(proc: subprocess.Popen, message: Json) -> None:
    data = json.dumps(message, ensure_ascii=False)
    content = f"Content-Length: {len(data.encode())}\r\n\r\n{data}"
    proc.stdin.write(content.encode())
    proc.stdin.flush()


def _mcp_read_exact(stream: Any, length: int, timeout: float) -> bytes:
    deadline = time.time() + timeout
    buf = b""
    fd = stream.fileno()
    while len(buf) < length:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("MCP read timeout")
        ready, _, _ = select.select([fd], [], [], min(remaining, 0.1))
        if ready:
            chunk = os.read(fd, length - len(buf))
            if not chunk:
                raise EOFError("MCP stream closed")
            buf += chunk
    return buf


def _mcp_read_message(proc: subprocess.Popen, timeout: float) -> Json:
    deadline = time.time() + timeout
    header = b""
    stdout_fd = proc.stdout.fileno()
    while b"\r\n\r\n" not in header:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("MCP header read timeout")
        ready, _, _ = select.select([stdout_fd], [], [], min(remaining, 0.1))
        if ready:
            byte = os.read(stdout_fd, 1)
            if not byte:
                raise EOFError("MCP stream closed")
            header += byte
    header_str = header.decode("utf-8", errors="replace")
    content_length = 0
    for line in header_str.split("\r\n"):
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
    if content_length == 0:
        raise ValueError("Missing Content-Length header")
    body = _mcp_read_exact(proc.stdout, content_length, deadline - time.time())
    return json.loads(body.decode("utf-8"))


def _mcp_request(proc: subprocess.Popen, method: str, params: Json | None = None, *, request_id: int = 1, timeout: float = 20) -> Json:
    message: Json = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        message["params"] = params
    _mcp_write_message(proc, message)
    response = _mcp_read_message(proc, timeout)
    if "error" in response:
        error = response["error"]
        raise ToolExecutionError(
            f"MCP error: {error.get('message', 'unknown')}",
            failure_type="execution_failed",
        )
    return response.get("result") or {}


def _mcp_notify(proc: subprocess.Popen, method: str, params: Json | None = None) -> None:
    message: Json = {"jsonrpc": "2.0", "method": method}
    if params:
        message["params"] = params
    _mcp_write_message(proc, message)


def _mcp_start(server: Json) -> subprocess.Popen:
    command = _mcp_command(server)
    env = _mcp_env(server)
    cwd = server.get("cwd") or None
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


def _mcp_initialize(proc: subprocess.Popen, server: Json, timeout: float, request_id: int = 1) -> None:
    _mcp_request(
        proc,
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "gateway", "version": "1.0"},
        },
        request_id=request_id,
        timeout=timeout,
    )
    _mcp_notify(proc, "notifications/initialized")


def _mcp_with_server(server: Json, fn: Callable[[subprocess.Popen, float], Any]) -> Any:
    session = _mcp_get_session(server)
    return fn(session.proc, session.timeout)


def _mcp_session_key(server: Json) -> str:
    name = str(server.get("name") or "")
    command = " ".join(_mcp_command(server))
    cwd = str(server.get("cwd") or "")
    return f"{name}:{cwd}:{command}"


def _mcp_use_pool(server: Json) -> bool:
    return server.get("pool", True)


def _mcp_get_session(server: Json) -> McpSession:
    key = _mcp_session_key(server)
    with MCP_SESSIONS_LOCK:
        session = MCP_SESSIONS.get(key)
        if session and session.proc.poll() is None:
            return session
        if session:
            session.close()
        try:
            session = McpSession(server)
        except Exception as exc:
            server_name = str(server.get("name") or key)
            _mcp_set_status(server_name, "broken", detail=str(exc), tool_count=0)
            MCP_SESSIONS.pop(key, None)
            MCP_TOOL_CATALOG_CACHE.pop(key, None)
            raise
        MCP_SESSIONS[key] = session
        return session


def _mcp_close_sessions() -> None:
    with MCP_SESSIONS_LOCK:
        for session in MCP_SESSIONS.values():
            session.close()
        MCP_SESSIONS.clear()


def _mcp_catalog_ttl(server: Json) -> float:
    return float(server.get("catalog_ttl") or MCP_CATALOG_CACHE_TTL_SECONDS)


def _mcp_cache_key(server: Json) -> str:
    return _mcp_session_key(server)


def _mcp_set_status(server_name: str, status: str, *, detail: str | None = None, tool_count: int | None = None) -> None:
    MCP_SERVER_STATUS[server_name] = {
        "status": status,
        "detail": detail,
        "tool_count": tool_count,
        "updated_at": time.time(),
    }


def _mcp_invalidate_server(server: Json, *, reason: str | None = None) -> None:
    key = _mcp_cache_key(server)
    MCP_TOOL_CATALOG_CACHE.pop(key, None)
    server_name = str(server.get("name") or "")
    if server_name:
        _mcp_set_status(server_name, "invalidated", detail=reason)


def _mcp_health_snapshot(*, probe: bool = False) -> list[Json]:
    result = []
    for server in _enabled_mcp_servers():
        name = str(server.get("name") or "")
        status = MCP_SERVER_STATUS.get(name, {})
        key = _mcp_cache_key(server)
        entry: Json = {
            "name": name,
            "status": status.get("status", "unknown"),
            "detail": status.get("detail"),
            "tool_count": status.get("tool_count"),
            "session": "connected" if key in MCP_SESSIONS else "not_connected",
            "cache": "hit" if key in MCP_TOOL_CATALOG_CACHE else "miss",
        }
        if probe:
            try:
                tools = _mcp_list_server_tools(server)
                entry["probe_ok"] = True
                entry["status"] = "ok"
                entry["tool_count"] = len(tools)
                entry["session"] = "connected" if key in MCP_SESSIONS else "not_connected"
                entry["cache"] = "hit" if key in MCP_TOOL_CATALOG_CACHE else "miss"
            except Exception as exc:
                entry["probe_ok"] = False
                entry["probe_error"] = str(exc)
                entry["status"] = "broken"
                entry["tool_count"] = 0
                entry["session"] = "not_connected"
                entry["cache"] = "miss"
        result.append(entry)
    return result


def _mcp_list_server_tools(server: Json) -> list[Json]:
    key = _mcp_cache_key(server)
    ttl = _mcp_catalog_ttl(server)
    cached = MCP_TOOL_CATALOG_CACHE.get(key)
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    server_name = str(server.get("name") or "")
    try:
        session = _mcp_get_session(server)
        result = session.request("tools/list")
    except Exception as exc:
        if server_name:
            _mcp_set_status(server_name, "broken", detail=str(exc), tool_count=0)
        MCP_TOOL_CATALOG_CACHE.pop(key, None)
        MCP_SESSIONS.pop(key, None)
        raise
    tools = result.get("tools") or []
    MCP_TOOL_CATALOG_CACHE[key] = (time.time(), tools)
    if server_name:
        _mcp_set_status(server_name, "ok", tool_count=len(tools))
    return tools


def _mcp_call_tool(server: Json, tool_name: str, arguments: Json) -> str:
    session = _mcp_get_session(server)
    result = session.request("tools/call", {"name": tool_name, "arguments": arguments})
    return _mcp_content_to_text(result)


def _mcp_content_to_text(result: Json) -> str:
    content = result.get("content") or []
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif item.get("type") == "image":
                parts.append("[image]")
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def _mcp_tool_schemas(path: str) -> list[Json]:
    schemas = []
    for server in _enabled_mcp_servers():
        server_name = str(server.get("name") or "")
        try:
            for tool in _mcp_list_server_tools(server):
                tool_name = str(tool.get("name") or "")
                description = tool.get("description") or f"MCP tool from {server_name}"
                parameters = tool.get("inputSchema") or {"type": "object", "properties": {}}
                for name in (_mcp_public_name(server_name, tool_name), _mcp_legacy_public_name(server_name, tool_name)):
                    if "/messages" in path:
                        schemas.append({"name": name, "description": description, "input_schema": parameters})
                    else:
                        schemas.append({
                            "type": "function",
                            "function": {"name": name, "description": description, "parameters": parameters},
                        })
        except Exception as exc:
            _mcp_set_status(server_name, "error", detail=str(exc))
    return schemas


def _tool_name_from_schema(path: str, item: Json) -> str | None:
    if "/messages" in path:
        return item.get("name")
    func = item.get("function")
    if isinstance(func, dict):
        return func.get("name")
    return item.get("name")


def _tool_schema_for_path(path: str, tool: "GatewayTool") -> Json:
    if "/messages" in path:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }
